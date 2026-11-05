from ..config import config
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server
from .hostvds import HostVDSClient
from .selectel import SelectelVDSClient

PROVIDERS = {"hostvds": "HostVDS", "selectel": "Selectel VDS"}


def make_client(provider: str, username: str, password: str, project_name: str):
    """Build a provider client from stored account fields (for Selectel the token is in `password`)."""
    if provider == "selectel":
        return SelectelVDSClient(password, config.selectel_api_url)
    return HostVDSClient(config.hostvds_auth_url, username, password, project_name)


__all__ = ["PROVIDERS", "make_client", "Flavor", "Image", "IpPool", "ProviderError", "RegionStatus", "Server"]
