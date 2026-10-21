"""Shared exceptions."""


class DnsServerError(Exception):
    """Raised when talking to a DNS server (AdGuard Home, Pi-hole) fails."""
