from ..config import config
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server
from .hostvds import HostVDSClient
from .aeza import AezaClient
from .selectel import SelectelVDSClient
from .timeweb import TimewebClient

PROVIDERS = {"hostvds": "HostVDS", "selectel": "Selectel VDS", "timeweb": "Timeweb Cloud", "aeza": "Aeza"}
# Providers without a subnet API: IP blocks are learned from servers and IP scouting.
SCOUT_PROVIDERS = {"selectel", "timeweb", "aeza"}
# Providers connected with a single API token (stored in Account.password).
TOKEN_PROVIDERS = {"selectel", "timeweb", "aeza"}


def make_client(provider: str, username: str, password: str, project_name: str):
    """Build a provider client from stored account fields (for Selectel the token is in `password`)."""
    if provider == "selectel":
        return SelectelVDSClient(password, config.selectel_api_url)
    if provider == "timeweb":
        return TimewebClient(password, config.timeweb_api_url)
    if provider == "aeza":
        return AezaClient(password, config.aeza_api_url)
    return HostVDSClient(config.hostvds_auth_url, username, password, project_name)


__all__ = ["PROVIDERS", "SCOUT_PROVIDERS", "TOKEN_PROVIDERS", "make_client", "Flavor", "Image", "IpPool", "ProviderError", "RegionStatus", "Server"]
