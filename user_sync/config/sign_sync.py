import logging
import codecs

from copy import deepcopy
from collections import defaultdict
from typing import Dict

from schema import Schema

from user_sync.config.common import DictConfig, ConfigLoader, ConfigFileLoader, resolve_invocation_options, validate_max_limit_config
from user_sync.error import AssertionException
from user_sync.engine.common import AdobeGroup
from user_sync.engine.sign import SignSyncEngine
from .error import ConfigValidationError
from ..helper import normalize_string


def config_schema() -> Schema:
    from schema import And, Optional, Or, Regex
    return Schema({
        'sign_orgs': {str: str},
        'identity_source': {
            'connector': And(str, len),
            'type': Or('csv', 'okta', 'ldap', 'adobe_console'),
        },
        'user_sync': {
            'sign_only_limit': Or(int, Regex(r'^\d+%$')),
            'sign_only_user_action': Or('exclude', 'reset', 'deactivate', 'remove_roles', 'remove_groups'),
            Optional('umg'): bool,
        },
        Optional('connection'): {
            Optional('request_concurrency'): int,
            Optional('batch_size'): int,
            Optional('retry_count'): int,
            Optional('timeout'): int
        },
        'user_management': [{
            'directory_group': Or(None, And(str, len)),
            Optional('sign_group', default=None): Or(None, list, And(str, len)),
            Optional('group_admin', default=False): Or(bool, None),
            Optional('admin_groups'): Or(None, [And(str, len)]),
            Optional('account_admin', default=False): Or(bool, None)
        }],
        Optional('primary_group_rules'): [{
            'sign_groups': [And(str, len)],
            'primary_group': And(str, len),
        }],
        Optional('account_admin_groups'): list,
        'cache': {
            'path': And(str, len),
        },
        Optional('logging'): {
            Optional('log_to_file'): bool,
            Optional('file_log_directory'): And(str, len),
            Optional('file_log_name_format'): And(str, len),
            Optional('file_log_level'): Or('info', 'debug'),
            Optional('console_log_level'): Or('info', 'debug'),
        },
        Optional('invocation_defaults'): {
            Optional('test_mode'):  bool,
            Optional('users'): Or('mapped', 'all', ['group', And(str, len)])
        }
    })


class SignConfigLoader(ConfigLoader):
    """
    Loads config files and does pathname expansion on settings that refer to files or directories
    """
    # key_paths in the root configuration file that should have filename values
    # mapped to their value options.  See load_from_yaml for the option meanings.
    ROOT_CONFIG_PATH_KEYS = {
        '/sign_orgs/*': (True, False, None),
        '/identity_source/connector': (True, False, None),
        '/logging/file_log_directory': (False, False, "sign_logs"),
        '/cache/path': (False, False, None),
    }

    # like ROOT_CONFIG_PATH_KEYS, but for non-root configuration files
    SUB_CONFIG_PATH_KEYS = {
        '/integration/priv_key_path': (True, False, None),
        '/file_path': (True, False, None),
    }

    config_defaults = {
        'config_encoding': 'utf8',
        'config_filename': 'sign-sync-config.yml',
    }

    invocation_defaults = {
        'users': ['mapped'],
        'test_mode': False
    }

    default_cache_path = "cache/sign"

    DEFAULT_ORG_NAME = 'primary'

    def __init__(self, args: dict):
        self.logger = logging.getLogger('sign_config')
        self.args = args
        filename, encoding = self._config_file_info()
        self.config_loader = ConfigFileLoader(encoding, self.ROOT_CONFIG_PATH_KEYS, self.SUB_CONFIG_PATH_KEYS)
        self.raw_config = self._load_raw_config(filename, encoding)
        self._validate(self.raw_config)
        self.main_config = self.load_main_config(filename, self.raw_config)
        self.invocation_options = self.load_invocation_options()
        self.directory_groups = self.load_directory_groups()

    def load_invocation_options(self) -> dict:
        options = deepcopy(self.invocation_defaults)
        invocation_config = self.main_config.get_dict_config('invocation_defaults', True)
        options = resolve_invocation_options(options, invocation_config, self.invocation_defaults, self.args)
        options['directory_connector_type'] = self.main_config.get_dict('identity_source').get('type')
        # --users
        users_spec = options.get('users')
        if users_spec:
            users_action = normalize_string(users_spec[0])
            if users_action == 'all':
                if options['directory_connector_type'] == 'okta':
                    raise AssertionException('Okta connector module does not support "--users all"')
            elif users_action == 'mapped':
                options['directory_group_mapped'] = True


            elif users_action == 'group':
                if len(users_spec) < 2:
                    raise AssertionException('You must specify the groups to read when using the users "group" option')
                dgf = users_spec[1].split(',') if len(users_spec) == 2 else users_spec[1:]
                options['directory_group_filter'] = list({d.strip() for d in dgf})
            else:
                raise AssertionException('Unknown option "%s" for users' % users_action)
        return options

    def load_main_config(self, filename, content) -> DictConfig:
        return DictConfig("<%s>" % filename, content)
    
    def _config_file_info(self) -> tuple[str, str]:
        filename = self.args.get('config_filename') or self.config_defaults['config_filename']
        encoding = self.args.get('encoding_name') or self.config_defaults['config_encoding']
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise AssertionException("Unknown encoding '%s' specified for configuration files" % encoding)
        return filename, encoding

    def _load_raw_config(self, filename, encoding) -> dict:
        self.logger.info("Using main config file: %s (encoding %s)", filename, encoding)
        return self.config_loader.load_root_config(filename)
    
    @staticmethod
    def _validate(raw_config: dict):
        from schema import SchemaError
        try:
            config_schema().validate(raw_config)
        except SchemaError as e:
            raise ConfigValidationError(e.code) from e

    def get_directory_groups(self):
        return self.load_directory_groups()

    def load_directory_groups(self) -> Dict[str, AdobeGroup]:
        group_mapping = {}
        group_config = self.main_config.get_list_config('user_management', True)
        if group_config is None:
            return group_mapping
        for i, mapping in enumerate(group_config.iter_dict_configs()):
            dir_group = mapping.get_string('directory_group')
            if dir_group not in group_mapping:
                group_mapping[dir_group] = {
                    'priority': i,
                    'groups': [],
                }

            sign_group = mapping.get_list('sign_group', True)
            if sign_group is None:
                continue
            for sg in sign_group:
                # AdobeGroup will return the same memory instance of a pre-existing group,
                # so we create it no matter what
                group = self._groupify(sg)

                # Note checking a memory equivalency, not group name
                # the groups in group_mapping are stored in order of YML file - important
                # for choosing first match for a user later
                if group not in group_mapping[dir_group]['groups']:
                    group_mapping[dir_group]['groups'].append(group)

        return group_mapping

    def load_account_admin_groups(self):
        account_admin_groups = set()
        using_deprecated_config = False
        group_config = self.main_config.get_list_config('user_management', True)
        for mapping in group_config.iter_dict_configs():
            dir_group = mapping.get_string('directory_group')
            is_admin = mapping.get_bool('account_admin', True)
            if is_admin is not None:
                using_deprecated_config = True
            if is_admin:
                account_admin_groups.add(dir_group)
        if using_deprecated_config:
            self.logger.warn("Deprecation warning: using 'account_admin' flag inside of group mapping is deprecated")
        admin_cfg_groups = self.main_config.get_list('account_admin_groups', True)
        if admin_cfg_groups is not None:
            for group in admin_cfg_groups:
                account_admin_groups.add(group)
        return list(account_admin_groups)

    def load_group_admin_mappings(self, umg):
        group_admin_mappings = {}
        group_config = self.main_config.get_list_config('user_management', True)
        for mapping in group_config.iter_dict_configs():
            dir_group = mapping.get_string('directory_group').lower()
            sign_group = mapping.get_list('sign_group', True)
            if sign_group is not None:
                sign_group = [sg.lower() for sg in sign_group]

            group_admin = mapping.get_bool('group_admin', True)
            if (sign_group is None or not len(sign_group)) and group_admin:
                # if there is no Sign group, add the directory group anyway
                # in case we're non-UMG, in which case the group admin status is
                # applied to the user's currently-assigned group
                if not umg:
                    group_admin_mappings[dir_group] = set()
                else:
                    raise AssertionException("If UMG is enabled, then at least one Sign group is required in a mapping that enables group admin")
                continue

            admin_groups = mapping.get_list('admin_groups', True)
            using_admin_groups = admin_groups is not None and len(admin_groups)

            # if group_admin flag is True and we're not using admin_groups list, then
            # map the first item in sign_group list and move on
            if group_admin and not using_admin_groups:
                group = self._groupify(sign_group[0])
                if dir_group not in group_admin_mappings:
                    group_admin_mappings[dir_group] = set()
                group_admin_mappings[dir_group].add(group)
                continue

            if not group_admin:
                continue

            if not using_admin_groups:
                continue

            if not umg:  #TODO non umg is broken ?
                self.logger.warn("Ignoring 'admin_groups' list because 'umg' mode is disabled")
                continue

            for ag in admin_groups:
                ag = ag.lower()
                if ag not in sign_group:
                    self.logger.warn("Skipping admin group '%s' because it isn't specified in 'sign_group'", ag)
                    continue
                if dir_group not in group_admin_mappings:
                    group_admin_mappings[dir_group] = set()
                group_admin_mappings[dir_group].add(self._groupify(ag))

        return group_admin_mappings

    def _groupify(self, group_name):
        """For a given group name, return AdobeGroup with proper primary
           target, error checking, etc"""
        group = AdobeGroup.create(group_name.lower())
        if group is None:
            raise AssertionException(f"Bad sign group '{group_name}' specified")
        if group.umapi_name is None:
            group.umapi_name = self.DEFAULT_ORG_NAME
        return group

    def load_primary_group_rules(self, umg):
        primary_group_rules = []
        if umg:
            group_config = self.main_config.get_list_config('primary_group_rules', True)
            for mapping in group_config.iter_dict_configs():
                sign_groups = mapping.get_list('sign_groups')
                primary_group = mapping.get_string('primary_group')
                primary_group_rules.append({
                    'sign_groups': set([g.lower() for g in sign_groups]),
                    'primary_group': primary_group
                })
        return primary_group_rules

    def get_directory_connector_module_name(self) -> str:
        # these .get()s can be safely chained because we've already validated the config schema
        return self.main_config.get_dict('identity_source').get('type')

    def get_directory_connector_options(self, name: str) -> dict:
        identity_config = self.main_config.get_dict('identity_source')
        source_name = identity_config['type']
        if name != source_name:
            raise AssertionException("requested identity source '{}' does not match configured source '{}'".format(source_name, name))
        source_config_path = identity_config['connector']
        return self.config_loader.load_sub_config(source_config_path)

    def get_target_options(self) -> dict[str, dict]:
        target_configs = self.main_config.get_dict('sign_orgs')
        if self.DEFAULT_ORG_NAME not in target_configs:
            raise AssertionException(f"'sign_orgs' config must specify a connector with '{self.DEFAULT_ORG_NAME}' key")
        primary_options = self.config_loader.load_sub_config(target_configs[self.DEFAULT_ORG_NAME])
        all_options = {}
        for target_id, config_file in target_configs.items():
            if target_id == self.DEFAULT_ORG_NAME:
                continue
            all_options[target_id] = self.config_loader.load_sub_config(config_file)
        all_options[self.DEFAULT_ORG_NAME] = primary_options
        return all_options

    def get_engine_options(self) -> dict:
        options = deepcopy(SignSyncEngine.default_options)
        options.update(self.invocation_options)

        sign_orgs = self.main_config.get_dict('sign_orgs')
        options['sign_orgs'] = sign_orgs
        user_sync = self.main_config.get_dict_config('user_sync')
        max_missing = user_sync.get_value('sign_only_limit', (int, str))
        options['user_sync']['sign_only_limit'] = validate_max_limit_config(max_missing)
        options['connection'] = self.main_config.get_dict('connection', True) or {}
        sign_only_user_action = user_sync.get_value('sign_only_user_action', (str, int))
        options['user_sync']['sign_only_user_action'] = sign_only_user_action
        options['user_sync']['umg'] = user_sync.get_bool('umg', True) or False
        if options.get('directory_group_mapped'):
            options['directory_group_filter'] = set(self.directory_groups.keys())
        options['cache'] = self.main_config.get_dict('cache')
        options['account_admin_groups'] = self.load_account_admin_groups()
        options['group_admin_mappings'] = self.load_group_admin_mappings(options['user_sync']['umg'])
        options['primary_group_rules'] = self.load_primary_group_rules(options['user_sync']['umg'])
        return options

    def check_unused_config_keys(self):
        # not clear if we need this since we are validating the config schema
        pass

    def get_logging_config(self) -> DictConfig:
        return self.main_config.get_dict_config('logging', True)

    def get_invocation_options(self):
        return self.invocation_options
