"""Integration boundaries for Shopy Store. NO external calls are made by this module.

Only enable provider adapters after owner approval, security review and real provider credentials.
COD remains active if any external provider is unavailable.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class IntegrationStatus:
    name: str
    configured: bool
    enabled: bool
    note: str

def available_integrations():
    return [
        IntegrationStatus('Cash on delivery', True, True, 'Customer pays on receipt.'),
        IntegrationStatus('Payment gateway', False, False, 'Requires merchant contract, webhooks, and verified API.'),
        IntegrationStatus('Safka', False, False, 'Requires marketer authorization and API key.'),
        IntegrationStatus('WhatsApp notifications', False, False, 'Requires official sender and approved template.'),
    ]
