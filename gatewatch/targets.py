import json
from dataclasses import dataclass
from ipaddress import ip_address


@dataclass(frozen=True)
class ActionTarget:
    node: str
    adapter: str
    jail: str
    ip: str

    @classmethod
    def access(cls, node, service, ip):
        jail = "sshd" if service in ("ssh", "sshd") else service
        return cls(node, "fail2ban", jail, str(ip_address(ip)))

    @classmethod
    def ssh(cls, node, ip):
        return cls.access(node, "sshd", ip)

    @classmethod
    def command(cls, node, adapter, ip):
        jail = "sshd" if adapter == "fail2ban" else ""
        return cls(node, adapter, jail, str(ip_address(ip)))

    @property
    def key(self):
        return "access:" + json.dumps((self.node, self.adapter, self.jail, self.ip), separators=(",", ":"))
