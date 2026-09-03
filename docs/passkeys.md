# Passkeys and the public PacketSafari URL

PacketSafari derives its WebAuthn configuration from the one canonical public
URL entered during on-prem onboarding. Customers do not configure a separate
WebAuthn origin or relying-party ID.

## Before onboarding

Prepare one DNS name and a valid TLS certificate for the address users will
open, for example:

```text
https://packetsafari.security.example
```

Enter that exact HTTPS origin in the **Public base URL** onboarding field. Do
not include a path, query, fragment, or credentials. PacketSafari then derives:

```text
WebAuthn origin: https://packetsafari.security.example
Relying-party ID: packetsafari.security.example
```

The installer generates and persists the MFA encryption key automatically.
Finalizing onboarding writes the derived values to the managed runtime env. A
stack restart activates them.

Passkeys require a secure browser context. Plain HTTP appliance access is not
a production passkey origin; use HTTPS. HTTP is accepted only for local
`localhost` development.

## Changing the URL later

An administrator can review or change the URL under **Admin → Operations →
On-prem → Public URL and passkeys**. PacketSafari blocks a hostname change while
any passkeys are registered because credentials are cryptographically bound to
the relying-party ID and would stop working at the new hostname.

To move to another hostname:

1. Ensure every account has another working authentication method.
2. Remove all registered passkeys.
3. Save the new canonical HTTPS URL in the admin deployment page.
4. Restart the PacketSafari stack through the normal operator procedure.
5. Ask users to enroll new passkeys on the new hostname.

Keep `/opt/packetsafari/env/runtime.env` in protected deployment backups. It
contains the stable MFA encryption key and must not be published or copied into
release artifacts.
