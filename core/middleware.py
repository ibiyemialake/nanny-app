"""
Keeps nannies' access limited to the home network.

How it works: every request's IP address is compared with STAFF_ALLOWED_NETWORKS
(for example 192.168.1.0/24, your home Wi-Fi). A signed-in nanny who shows up
from anywhere else is signed out and sees a "connect to the home Wi-Fi" page.
The owner account is never restricted.

This is a second layer. The first, stronger layer is hosting the app only on
your home network so it can't be reached from outside at all (see README).
"""
import ipaddress

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import render

from .permissions import get_role


def client_ip(request):
    if settings.TRUST_X_FORWARDED_FOR:
        # The last address is the one our own proxy added. Earlier ones come from the client and can be faked.
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def ip_allowed(ip):
    networks = settings.STAFF_ALLOWED_NETWORKS
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    for network in networks:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            continue
    return False


class HomeNetworkMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.client_ip = client_ip(request)
        request.on_home_network = ip_allowed(request.client_ip)
        if not request.on_home_network and get_role(getattr(request, "user", None)) == "staff":
            logout(request)
            return render(request, "core/network_blocked.html", status=403)
        return self.get_response(request)
