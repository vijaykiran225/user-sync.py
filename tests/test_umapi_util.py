import pytest

from user_sync.config.common import ConfigFileLoader, DictConfig
from user_sync.config.user_sync import UMAPIConfigLoader
from user_sync.connector.umapi_util import make_auth_dict
from user_sync.error import AssertionException
import user_sync.connector.helper


def test_make_auth_dict(test_resources):
    config_loader = ConfigFileLoader('utf-8', UMAPIConfigLoader.ROOT_CONFIG_PATH_KEYS, UMAPIConfigLoader.SUB_CONFIG_PATH_KEYS)
    umapi_config = config_loader.load_from_yaml(test_resources['umapi'], {})
    umapi_config['enterprise']['priv_key_path'] = test_resources['priv_key']
    # note that the private_key fixture is actually just the absolute path to test_private.key in the fixture dir
    umapi_dict_config = DictConfig('enterprise', umapi_config['enterprise'])
    org_id_from_file = umapi_config['enterprise']['org_id']
    tech_acct_from_file = umapi_config['enterprise']['tech_acct_id']
    api_key_from_file = umapi_config['enterprise']['client_id']
    client_secret_from_file = umapi_config['enterprise']['client_secret']
    logger = user_sync.connector.helper.create_logger({})
    name = 'umapi'
    auth_dict = make_auth_dict(name, umapi_dict_config, org_id_from_file, tech_acct_from_file, logger)
    assert auth_dict['org_id'] == org_id_from_file
    assert auth_dict['tech_acct_id'] == tech_acct_from_file
    assert auth_dict['api_key'] == api_key_from_file
    assert auth_dict['client_secret'] == client_secret_from_file
    with open(test_resources['priv_key']) as f:
        key_data_from_file = f.read()
    assert auth_dict['private_key_data'] == key_data_from_file
    # add priv_key_data along with path and check for the exception
    umapi_config['enterprise']['priv_key_data'] = key_data_from_file
    with pytest.raises(AssertionException):
        make_auth_dict(name, umapi_dict_config, org_id_from_file, tech_acct_from_file, logger)
    # now set the path to none and make sure that auth dict will still return the key data correctly
    umapi_config['enterprise']['priv_key_path'] = None
    auth_dict_key_data = make_auth_dict(name, umapi_dict_config, org_id_from_file, tech_acct_from_file, logger)
    assert auth_dict_key_data['private_key_data'] == key_data_from_file
    # if there are settings for both priv_key_data and secure_priv_key_data_key make_auth_dict throws the same
    # AssertionException thrown by get_credential (from the DictConfig)
    umapi_config['enterprise']['secure_priv_key_data_key'] = 'make_auth_identifier'
    with pytest.raises(AssertionException):
        make_auth_dict(name, umapi_dict_config, org_id_from_file, tech_acct_from_file, logger)