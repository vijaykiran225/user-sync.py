import logging
import time

from user_sync.config.common import DictConfig, ConfigFileLoader, as_set, check_max_limit
from user_sync.connector.connector_sign import SignConnector
from user_sync.error import AssertionException
from sign_client.error import AssertionException as ClientException

from sign_client.model import DetailedUserInfo, GroupInfo, UserGroupsInfo, UserGroupInfo, DetailedGroupInfo, UserStateInfo
from .common import AdobeGroup


class SignSyncEngine:
    default_options = {
        'directory_group_filter': None,
        'identity_source': {
            'type': 'ldap',
            'connector': 'connector-ldap.yml'
        },
        'invocation_defaults': {
            'users': 'mapped',
            'test_mode': False
        },
        'cache': {
            'path': 'cache/sign',
        },
        'sign_orgs': [
            {
                'primary': 'connector-sign.yml'
            }
        ],
        'user_sync': {
            'sign_only_limit': 100,
            'sign_only_user_action': 'reset'
        }
    }

    name = 'sign_sync'
    encoding = 'utf-8'

    def __init__(self, caller_options, target_options: dict[str, dict]):
        """
        Initialize the Sign Sync Engine
        :param caller_options:
        :return:
        """
        super().__init__()
        options = dict(self.default_options)
        options.update(caller_options)
        self.options = options
        self.logger = logging.getLogger(self.name)
        self.directory_user_by_user_key = {}
        self.connectors: dict[str, SignConnector] = {}
        self.default_groups = {}
        self.sign_groups = {}
        self.sign_user_groups = {}
        self.caller_options = caller_options
        self.target_options = target_options
        self.action_summary = {}
        self.sign_users_by_org: dict[str, dict[str, DetailedUserInfo]] = {}
        self.total_sign_user_count = 0
        self.sign_users_created = set()
        self.sign_users_deactivated = set()
        self.sign_admins_matched = set()
        self.sign_users_matched_groups = set()
        self.sign_users_group_updates = set()
        self.sign_users_role_updates = set()
        self.sign_users_matched_no_updates = set()
        self.directory_users_excluded = set()
        self.sign_only_users_by_org: dict[str, dict[str, DetailedUserInfo]] = {}
        self.target_groups_by_org = {}
        self.total_sign_only_user_count = 0

    def get_groups(self, org):
        return self.connectors[org].sign_groups()

    def get_default_group(self, org):
        print(org, "passed to me")
        print(self.connectors, "passed to connector")
        print(self.connectors[org], "passed to connector[org]")
        print(self.connectors[org].sign_groups(), "passed to connector[org].sg")
        print(self.connectors[org].sign_groups().values())
        return [g for g in self.connectors[org].sign_groups().values() if g.isDefaultGroup][0]

    def run(self, directory_groups, directory_connector):
        """
        Run the Sign sync
        :param directory_groups:
        :param directory_connector:
        :return:
        """
        self.read_desired_user_groups(directory_groups, directory_connector)

        for org_name, target_dict in self.target_options.items():
            self.connectors[org_name] = SignConnector(target_dict, org_name, self.options['test_mode'], self.caller_options['connection'], self.caller_options['cache'])
        
        for org_name in self.connectors:
            print(org_name,"is the org name !")
            self.sign_groups[org_name] = self.get_groups(org_name)
            self.default_groups[org_name] = self.get_default_group(org_name)
            self.target_groups_by_org[org_name] = set([group for groups in [g['groups']
                                                                            for g in directory_groups.values()]
                                                       for group in groups if group.umapi_name == org_name])

        for org_name, sign_connector in self.connectors.items():
            self.sign_user_groups[org_name] = sign_connector.get_user_groups()
            # Create any new Sign groups
            org_directory_groups = self._groupify(
                org_name, directory_groups.values())
            for directory_group in org_directory_groups:
                if (directory_group.lower() not in self.sign_groups[org_name]):
                    self.logger.info(
                        "{}Creating new Sign group: {}".format(self.org_string(org_name), directory_group))
                    sign_connector.create_group(DetailedGroupInfo(name=directory_group))
            self.sign_groups[org_name] = self.get_groups(org_name)
            # Update user details or insert new user
            self.update_sign_users(
                self.directory_user_by_user_key, sign_connector, org_name)
            print("VVVVVVVV  VVVVVVVV  VVVVVVVV  self.sign_only_users_by_org is ",self.sign_only_users_by_org)
            if org_name in self.sign_only_users_by_org:
                self.handle_sign_only_users(sign_connector, org_name)
        self.log_action_summary()

    def log_action_summary(self):
        self.action_summary = {
            'Number of directory users read': len(self.directory_user_by_user_key),
            'Number of directory selected for input': len(self.directory_user_by_user_key) - len(
                self.directory_users_excluded),
            'Number of directory users excluded': len(self.directory_users_excluded),
            'Number of Sign users read': self.total_sign_user_count,
            'Number of Sign users not in directory (sign-only)': self.total_sign_only_user_count,
            'Number of Sign users updated': len(self.sign_users_group_updates | self.sign_users_role_updates),
            'Number of users with groups updated': len(self.sign_users_group_updates),
            'Number of users admin roles updated': len(self.sign_users_role_updates),
            'Number of Sign users created': len(self.sign_users_created),
            'Number of Sign users deactivated': len(self.sign_users_deactivated),
        }

        pad = max(len(k) for k in self.action_summary)
        header = '------- Action Summary -------'
        self.logger.info('---------------------------' + header + '---------------------------')
        for description, count in self.action_summary.items():
            self.logger.info('  {}: {}'.format(description.rjust(pad, ' '), count))

    def update_sign_users(self, directory_users, sign_connector: SignConnector, org_name):
        """
        Updates user details or inserts new user
        :param directory_groups:
        :param sign_connector:
        :param org_name:
        :return:
        """
        # Fetch the list of active Sign users
        sign_users = {user.email: user for user in sign_connector.get_users().values() if user.status != 'INACTIVE'}
        inactive_sign_users = {user.email: user for user in sign_connector.get_users().values() if user.status == 'INACTIVE'}
        users_update_list = []
        user_groups_update_list = []
        dir_users_for_org = {}
        self.total_sign_user_count += len(sign_users)
        self.sign_users_by_org[org_name] = sign_users
        for directory_user_key, directory_user in directory_users.items():
            print(f"vvvvvvvvvv inside for loop'{directory_user}")
            if not self.should_sync(directory_user, org_name):
                print(f"vvvvvvvvvv skipping for loop'{directory_user_key}")
                continue

            sign_user = sign_users.get(directory_user_key)
            dir_users_for_org[directory_user_key] = directory_user
            assignment_groups = [g for g in directory_user['sign_groups'] if g.umapi_name == org_name]
            print(f"vvvvvvvvvv got this user from get'{sign_user} , {directory_user_key}")
            if not assignment_groups:
                assignment_groups = [AdobeGroup(self.default_groups[org_name].groupName, org_name)]

            if sign_user is None:
                if sign_connector.create_users:
                    inactive_user = inactive_sign_users.get(directory_user_key)
                    # if Standalone user is inactive, we need to reactivate instead of trying to create new account
                    if inactive_user is not None:
                        try:
                            print(f"vvvvvvvvvv about to update user '{inactive_user}")
                            state = UserStateInfo(
                                state='ACTIVE',
                                comment='Activated by User Sync Tool'
                            )
                            sign_connector.update_user_state(inactive_user.id, state)
                            self.logger.info(f"Reactivated user '{inactive_user.email}")
                        except ClientException as e:
                            self.logger.error(f"Reactivation error for '{inactive_user.email}: "+format(e))
                    else:
                        # if user is totally new then create it
                        print(f"vvvvvvvvvv about to create user '{inactive_user}")
                        self.insert_new_users(
                            org_name, sign_connector, directory_user, assignment_groups)
                else:
                    self.logger.info("{0}User {1} not present and will be skipped."
                                     .format(self.org_string(org_name), directory_user['email']))
                    self.directory_users_excluded.add(directory_user['email'])
                    continue
            else:
                is_umg = self.options['user_sync']['umg']
                # do not update if admin status should not change
                if sign_user.isAccountAdmin != directory_user['is_admin']:
                    # Update existing users
                    if directory_user['is_admin']:
                        self.logger.info(f"Assigning account admin status to {sign_user.email}")
                    else:
                        self.logger.info(f"Removing account admin status from {sign_user.email}")
                    user_data = DetailedUserInfo(**sign_user.__dict__)
                    user_data.isAccountAdmin = directory_user['is_admin']
                    self.sign_users_role_updates.add(sign_user.email)
                    users_update_list.append(user_data)

                # manage primary group asssignment
                current_groups = self.sign_user_groups[org_name].get(sign_user.id)

                assigned_groups = {}
                if current_groups is not None:
                    assigned_groups = {g.name.lower(): g for g in current_groups}
                if not is_umg:
                    g = self.get_primary_group(sign_user, self.sign_user_groups[org_name])
                    assigned_groups = {g.name.lower(): g}

                desired_groups = set()
                if directory_user['sign_groups']:
                    desired_groups = set([g.group_name.lower() for g in directory_user['sign_groups']])
                else:
                    desired_groups = set([self.get_primary_group(sign_user, self.sign_user_groups[org_name]).name.lower()])
                if not is_umg:
                    desired_groups = set([directory_user['sign_group'][0].group_name.lower()])

                groups_to_update = {}
                admin_groups = set([g.group_name for g in directory_user['admin_groups'] if g.umapi_name == org_name])

                # identify groups to add for user
                groups_to_assign = desired_groups.difference(set(assigned_groups.keys()))
                for group_name in groups_to_assign:
                    group_info = self.sign_groups[org_name].get(group_name)
                    if group_info is None:
                        raise AssertionException(f"'{group_name}' isn't a valid Sign group")

                    is_group_admin = ((not is_umg and directory_user['is_admin_group'])
                                      or (group_name in admin_groups))
                    groups_to_update[group_name] = UserGroupInfo(
                        id=group_info.groupId,
                        name=group_info.groupName,
                        isGroupAdmin=is_group_admin,
                        isPrimaryGroup=False,
                        status='ACTIVE',
                    )
                    self.logger.info(f"Assigning group '{group_info.groupName}' to user {sign_user.email}")
                    if group_name in admin_groups:
                        self.logger.info(f"Assigning group admin privileges to user {sign_user.email} for group '{group_info.groupName}'")

                # identify groups to remove for user
                target_groups = set([g.group_name.lower() for g in self.target_groups_by_org[org_name]])
                assigned_group_names = set(assigned_groups.keys())
                # first, get groups that are assigned but not in the desired list
                # then see what that has in common with overall target groups - this is the list
                # of groups to remove. non-targeted groups remain untouched
                remove_groups = target_groups.intersection(assigned_group_names.difference(desired_groups))
                for group_name in remove_groups:
                    # this should never happen but we need to check
                    if group_name in groups_to_assign:
                        raise AssertionException(f"Cannot remove group '{group_name}' because it is in the assignment list")
                    group_info = self.sign_groups[org_name].get(group_name)
                    if group_info is None:
                        raise AssertionException(f"'{group_name}' isn't a valid Sign group")

                    assigned_group = assigned_groups.get(group_info.groupName.lower())

                    groups_to_update[group_name] = UserGroupInfo(
                        id=group_info.groupId,
                        name=group_info.groupName,
                        isGroupAdmin=assigned_group.isGroupAdmin,
                        isPrimaryGroup=assigned_group.isPrimaryGroup,
                        status='DELETED',
                    )
                    self.logger.info(f"Removing group '{group_info.groupName}' for user {sign_user.email}")

                # get a full list of groups the user is an admin for
                current_admin_groups = set([g.name.lower() for g in assigned_groups.values() if g.isGroupAdmin]).\
                    union(set([g.name.lower() for g in groups_to_update.values() if g.status == 'ACTIVE' and g.isGroupAdmin]))

                # if a user is group admin to any group they're not mapped to be admin for, then they
                # need to have their status removed
                non_admin_groups = current_admin_groups.difference(set([g.group_name for g in directory_user['admin_groups']]))

                for group_name in non_admin_groups:
                    if group_name in groups_to_update:
                        groups_to_update[group_name].isGroupAdmin = False
                    else:
                        group_info = assigned_groups.get(group_name)
                        groups_to_update[group_name] = UserGroupInfo(
                            id=group_info.id,
                            name=group_info.name,
                            isGroupAdmin=False,
                            isPrimaryGroup=group_info.isPrimaryGroup,
                            status='DELETED',
                        )

                # figure out primary group for user
                sign_groups = set([g.lower() for g in groups_to_update.keys()])\
                              .union(set([g.lower() for g in assigned_groups.keys()]))
                desired_pg = self.resolve_primary_group(sign_groups)
                current_pg = [g.name.lower() for g in assigned_groups.values() if g.isPrimaryGroup]
                if current_pg:
                    current_pg = current_pg[0]
                else:
                    current_pg = None

                if desired_pg is None:
                    raise AssertionException(f"Can't identify a primary group for user '{sign_user.email}'")

                if current_pg is None or desired_pg.lower() != current_pg:
                    self.logger.debug(f"Primary group of '{sign_user.email}' is '{desired_pg}'")
                    print("\r\r\r\n\n\n",groups_to_update," is the groups_to_update")
                    groups_to_update[desired_pg.lower()].isPrimaryGroup = True

                if groups_to_update:
                    group_update_data = UserGroupsInfo(groupInfoList=list(groups_to_update.values()))
                    user_groups_update_list.append((sign_user.id, group_update_data))

        sign_connector.update_users(users_update_list)
        sign_connector.update_user_groups(user_groups_update_list)
        self.sign_only_users_by_org[org_name] = {}
        for user, data in sign_users.items():
            if user not in dir_users_for_org:
                self.total_sign_only_user_count += 1
                self.sign_only_users_by_org[org_name][user] = data

    def resolve_primary_group(self, sign_groups):
        print("\r\r\r\n\n\nVVVVVVVVVVV sign_groups i got is ",sign_groups,"\r\r\r\n\n\n")
        rules = self.options['primary_group_rules']
        print("\r\r\r\n\n\nVVVVVVVVVVV rules i got is ",rules,"\r\r\r\n\n\n")
        for r in rules:
            if set(sign_groups).intersection(r['sign_groups']) == r['sign_groups']:
                return r['primary_group']

    @staticmethod
    def get_primary_group(user, sign_user_groups) -> UserGroupInfo:
        user_groups = sign_user_groups.get(user.id)
        if user_groups:
            return [g for g in user_groups if g.isPrimaryGroup][0]

    @staticmethod
    def roles_match(resolved_roles, sign_roles) -> bool:
        """
        Checks if the existing user role in Sign Console is same as in configuration
        :param resolved_roles:
        :param sign_roles:
        :return:
        """
        return as_set(resolved_roles) == as_set(sign_roles)

    @staticmethod
    def should_sync(directory_user, org_name) -> bool:
        """
        Check if the user belongs to org.  If user has NO groups specified,
        we assume primary and return True (else we cannot assign roles without
        groups)
        :param umapi_user:
        :param org_name:
        :return:
        """
        print("VVVVVVvvvvvv directory_user['sign_groups'] is ", directory_user['sign_groups'])
        print("VVVVVVvvvvvv org_name is ", org_name)
        if not directory_user['sign_groups']:
            return True
        groups = [g for g in directory_user['sign_groups'] if g.umapi_name == org_name]
        return len(groups) > 0

    @staticmethod
    def _groupify(org_name, groups):
        """
        Extracts the Sign Group name from the configuration for an org
        :param org_name:
        :param groups:
        :return:
        """
        processed_groups = []
        for group_dict in groups:
            for group in group_dict['groups']:
                group_name = group.group_name
                if (org_name == group.umapi_name):
                    processed_groups.append(group_name)
        return processed_groups

    def read_desired_user_groups(self, mappings, directory_connector):
        """
        Reads and loads the users and group information from the identity source
        :param mappings:
        :param directory_connector:
        :return:
        """
        self.logger.debug('Building work list...')

        options = self.options
        directory_group_filter = options['directory_group_filter']
        if directory_group_filter is not None:
            directory_group_filter = set(directory_group_filter)
        directory_user_by_user_key = self.directory_user_by_user_key

        directory_groups = set(mappings.keys())
        if directory_group_filter is not None:
            directory_groups.update(directory_group_filter)
        directory_users = directory_connector.load_users_and_groups(groups=directory_groups,
                                                                    extended_attributes=[],
                                                                    all_users=directory_group_filter is None)

        for directory_user in directory_users:
            if not self.is_directory_user_in_groups(directory_user, directory_group_filter):
                continue

            user_key = self.get_directory_user_key(directory_user)
            if not user_key:
                self.logger.warning(
                    "Ignoring directory user with empty user key: %s", directory_user)
                continue
            sign_groups, is_admin, is_group_admin, admin_groups = \
                self.resolve_group_mappings(directory_user['groups'], mappings,
                                            self.options['account_admin_groups'],
                                            self.options['group_admin_mappings'])
            directory_user['sign_groups'] = sign_groups
            directory_user['is_admin'] = is_admin
            directory_user['is_group_admin'] = is_group_admin
            directory_user['admin_groups'] = admin_groups
            directory_user_by_user_key[user_key] = directory_user

    def is_directory_user_in_groups(self, directory_user, groups):
        """
        :type directory_user: dict
        :type groups: set
        :rtype bool
        """
        if groups is None:
            return True
        for directory_user_group in directory_user['groups']:
            if directory_user_group in groups:
                return True
        return False

    def get_directory_user_key(self, directory_user):
        """
        :type directory_user: dict
        """
        email = directory_user.get('email')
        if email:
            return str(email).lower()
        return None

    @staticmethod
    def resolve_group_mappings(directory_groups, group_mapping, account_admin_groups, group_admin_mapping) -> dict:
        matched_groups = set()

        matched_mappings = [m for g, m in group_mapping.items() if g in directory_groups]
        matched_mappings.sort(key=lambda x: x['priority'])

        for m in matched_mappings:
            if m['groups']:
                for g in m['groups']:
                    matched_groups.add(g)

        is_admin = False
        for g in directory_groups:
            if g in account_admin_groups:
                is_admin = True
                break

        # for non-UMG we don't care which group we make admin. we apply the
        # setting to the user's currently-assigned group (which may change
        # in the group mapping)
        is_group_admin = False
        admin_groups = set()
        for dir_group, target_groups in group_admin_mapping.items():
            if dir_group in directory_groups:
                is_group_admin = True
                admin_groups.update(target_groups)

        return list(matched_groups), is_admin, is_group_admin, admin_groups

    def insert_new_users(self, org_name: str, sign_connector: SignConnector, directory_user: dict, assignment_groups):
        """
        Constructs the data for insertion and inserts new user in the Sign Console
        """
        new_user = DetailedUserInfo(
            accountType='GLOBAL', # ignored on POST
            email=directory_user['email'],
            id='', # required, but not set by the user
            isAccountAdmin=directory_user['is_admin'],
            status='ACTIVE',
            firstName=directory_user['firstname'],
            lastName=directory_user['lastname'],
        )
        try:
            is_umg = self.options['user_sync']['umg']
            if is_umg:
                groups = assignment_groups
            else:
                groups = assignment_groups[0:1]
            groups_to_assign = {}
            for group in groups:
                wants_group_admin = False
                if is_umg:
                    wants_group_admin = directory_user['is_group_admin']
                else:
                    wants_group_admin = group in directory_user['admin_groups']
                group_to_assign = self.sign_groups[org_name][group.group_name.lower()]
                groups_to_assign[group_to_assign.groupName.lower()] = UserGroupInfo(
                    id=group_to_assign.groupId,
                    name=group_to_assign.groupName,
                    isGroupAdmin=wants_group_admin,
                    isPrimaryGroup=False,
                    status='ACTIVE',
                )
                self.logger.info(f"{self.org_string(sign_connector.console_org)}Assigning '{new_user.email}' to group '{group_to_assign.groupName}', group admin?: {wants_group_admin}")
            primary_group = self.resolve_primary_group(groups_to_assign.keys())
            if primary_group is None:
                raise AssertionException(f"Can't identify a primary group for user '{new_user.email}'")
            self.logger.debug(f"Primary group of '{new_user.email}' is '{primary_group}'")
            groups_to_assign[primary_group.lower()].isPrimaryGroup = True
            user_id = sign_connector.insert_user(new_user)
            self.sign_users_created.add(directory_user['email'])
            self.logger.info(f"{self.org_string(sign_connector.console_org)}Inserted sign user '{new_user.email}', admin?: {new_user.isAccountAdmin}")

            group_update_data = UserGroupsInfo(groupInfoList=list(groups_to_assign.values()))
            sign_connector.update_user_group_single(user_id, group_update_data)
        except ClientException as e:
            self.logger.error(format(e))

    def handle_sign_only_users(self, sign_connector: SignConnector, org_name: str):
        """
        Searches users to set to default group in GPS and deactivate in the Sign Neptune console
        :param directory_users:
        :param sign_connector:
        :param sign_user:
        :param org_name:
        :param default_group:
        :return:
        """

        # This will check the limit settings and log a message if the limit is exceeded
        if not self.check_sign_max_limit(org_name):
            return

        sign_only_user_action = self.options['user_sync']['sign_only_user_action']
        users_update_list = []
        groups_update_list = []
        for user in self.sign_only_users_by_org[org_name].values():
            if sign_only_user_action == 'exclude':
                self.logger.debug(
                    f"Sign user '{user.email}' was excluded from sync. sign_only_user_action: set to '{sign_only_user_action}'")
                continue
            elif sign_connector.deactivate_users and sign_only_user_action == 'deactivate':
                try:
                    state = UserStateInfo(
                        state='INACTIVE',
                        comment='Deactivated by User Sync Tool'
                    )
                    sign_connector.update_user_state(user.id, state)
                    self.sign_users_deactivated.add(user.email)
                    self.logger.info(f"{self.org_string(org_name)}Deactivated sign user '{user.email}'")
                except ClientException as e:
                    self.logger.error(format(e))
                continue

            in_default_group = self.get_primary_group(user, self.sign_user_groups[org_name]) == self.default_groups[org_name].groupId
            is_group_admin = self.get_primary_group(user, self.sign_user_groups[org_name]).isGroupAdmin

            if in_default_group and not is_group_admin and not user.isAccountAdmin:
                continue

            # set up group update in case we end up making one
            new_user_group = UserGroupInfo(
                id=self.get_primary_group(user, self.sign_user_groups[org_name]).id,
                isGroupAdmin=self.get_primary_group(user, self.sign_user_groups[org_name]).isGroupAdmin,
                isPrimaryGroup=True,
                status='ACTIVE',
            )
            if sign_only_user_action == 'reset':
                new_user_group.id = self.default_groups[org_name].groupId
                new_user_group.isGroupAdmin = False
                self.logger.info(f"{self.org_string(org_name)}Resetting '{user.email}' to Default Group and removing group admin status")
                groups_update_list.append((user.id, UserGroupsInfo(groupInfoList=[new_user_group])))
            if sign_only_user_action == 'remove_roles' and is_group_admin:
                new_user_group.isGroupAdmin = False
                self.logger.info(f"{self.org_string(org_name)}Removing group admin status for user '{user.email}'")
                groups_update_list.append((user.id, UserGroupsInfo(groupInfoList=[new_user_group])))
            if sign_only_user_action == 'remove_groups' and not in_default_group:
                new_user_group.id = self.default_groups[org_name].groupId
                self.logger.info(f"{self.org_string(org_name)}Resetting '{user.email}' to Default Group")
                groups_update_list.append((user.id, UserGroupsInfo(groupInfoList=[new_user_group])))

            # remove admin status if needed
            if sign_only_user_action in ['remove_roles', 'reset'] and user.isAccountAdmin:
                    user_update = DetailedUserInfo(**user.__dict__)
                    user_update.isAccountAdmin = False
                    self.logger.info(f"{self.org_string(org_name)}Removing account admin status for user '{user.email}'")
                    users_update_list.append(user_update)

        sign_connector.update_users(users_update_list)
        sign_connector.update_user_groups(groups_update_list)

    def check_sign_max_limit(self, org_name):
        stray_count = len(self.sign_only_users_by_org[org_name])
        sign_only_limit = self.options['user_sync']['sign_only_limit']
        return check_max_limit(stray_count, sign_only_limit, self.total_sign_user_count, 0, 'Sign', self.logger,
                               self.org_string(org_name))

    def org_string(self, org):
        return "Org: {} - ".format(org.capitalize()) if len(self.connectors) > 1 else ""
