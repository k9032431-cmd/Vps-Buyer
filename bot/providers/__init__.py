from ..config import config
from .aeza import AezaClient
from .base import Flavor, Image, IpPool, ProviderError, RegionStatus, Server
from .civo import CivoClient
from .digitalocean import DigitalOceanClient
from .gcp import GCPClient
from .hetzner import HetznerClient
from .hostvds import HostVDSClient
from .linode import LinodeClient
from .ovh import OVHClient
from .selectel import SelectelVDSClient
from .serverspace import ServerspaceClient
from .timeweb import TimewebClient
from .vultr import VultrClient

# Shown in the bot (buy / accounts menus), in this order.
PROVIDERS = {"hostvds": "HostVDS", "digitalocean": "DigitalOcean", "hetzner": "Hetzner", "vultr": "Vultr",
             "linode": "Linode", "civo": "Civo", "gcp": "Google Cloud", "ovh": "OVHcloud"}
# Every provider the bot can still talk to — hidden ones stay so servers bought there can be deleted.
PROVIDER_NAMES = {**PROVIDERS, "selectel": "Selectel VDS", "timeweb": "Timeweb Cloud", "aeza": "Aeza",
                  "serverspace": "Serverspace"}
# Providers without a subnet list: blocks are learned from servers and IP scouting.
SCOUT_PROVIDERS = {"selectel", "timeweb", "aeza", "serverspace", "hetzner", "vultr", "civo", "gcp", "ovh"}
# Providers that reserve a standalone IP and build a server on it (fast, almost free IP hunting / scouting).
RESERVE_PROVIDERS = {"hetzner", "vultr", "civo", "gcp"}
# Providers connected with a single API token (stored in Account.password).
TOKEN_PROVIDERS = {"selectel", "timeweb", "aeza", "serverspace", "digitalocean", "hetzner", "vultr", "linode", "civo",
                   "gcp"}
# Providers whose full subnet list is known up front from a geofeed (prefixes are CIDRs, chosen by mask).
SUBNET_PROVIDERS = {"digitalocean", "hetzner", "vultr", "linode"}
# Providers that can put your SSH public key on a new server (Aeza's order API has no way to pass one).
SSH_KEY_PROVIDERS = {"hostvds", "selectel", "timeweb", "serverspace", "digitalocean", "hetzner", "vultr",
                     "linode", "civo", "gcp", "ovh"}

_CLIENTS = {
    "selectel": lambda t: SelectelVDSClient(t, config.selectel_api_url),
    "timeweb": lambda t: TimewebClient(t, config.timeweb_api_url),
    "aeza": lambda t: AezaClient(t, config.aeza_api_url),
    "serverspace": lambda t: ServerspaceClient(t, config.serverspace_api_url),
    "digitalocean": lambda t: DigitalOceanClient(t, config.do_api_url),
    "hetzner": lambda t: HetznerClient(t, config.hetzner_api_url),
    "vultr": lambda t: VultrClient(t, config.vultr_api_url),
    "linode": lambda t: LinodeClient(t, config.linode_api_url),
    "civo": lambda t: CivoClient(t, config.civo_api_url),
    "gcp": lambda t: GCPClient(t),
}


def make_client(provider: str, username: str, password: str, project_name: str):
    """Build a provider client from stored account fields (token providers keep the token in `password`)."""
    if provider in _CLIENTS:
        return _CLIENTS[provider](password)
    if provider == "ovh":
        return OVHClient(config.ovh_auth_url, username, password, project_name)
    return HostVDSClient(config.hostvds_auth_url, username, password, project_name)


__all__ = ["PROVIDERS", "PROVIDER_NAMES", "RESERVE_PROVIDERS", "SCOUT_PROVIDERS", "SUBNET_PROVIDERS", "SSH_KEY_PROVIDERS",
           "TOKEN_PROVIDERS", "make_client", "Flavor", "Image", "IpPool", "ProviderError", "RegionStatus", "Server"]
