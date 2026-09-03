# Security Content Delivery

PacketSafari uses a hybrid content model. Application releases carry only
content PacketSafari has approved for redistribution; vendor and customer
content stays under the customer's own acquisition and licensing boundary.

## Included in the signed baseline

The normal `scripts/build_local_release.py` flow automatically builds and signs
the release content pack from the reviewed PacketSafari build spec before it
builds application images. The required baseline contains:

- PacketSafari-authored Suricata rules;
- reviewed ET Open and OISF Traffic ID inputs with complete notices: the
  GPL-2.0/BSD notices shipped in the ET Open archive and the MIT notice in the
  Traffic ID rule file;
- the reviewed MIT cloud/CDN taxonomy inputs from
  `tobilg/public-cloud-provider-ip-ranges` and `projectdiscovery/cdncheck`.

Threat-intelligence snapshots and external JA4 intelligence are optional. Their
absence does not block a release, but the application reports the corresponding
enrichment as unavailable or partial. A removed optional signed package is also
removed from runtime; stale content is not silently retained.

## Vendor-direct and customer-provided sources

abuse.ch SSLBL/URLhaus, Stamus Lateral, Spamhaus DROP, and FoxIO JA4+ data are
not redistributed in PacketSafari's baseline under this policy. Connected
deployments may enable an approved HTTPS source after reviewing the provider's
terms and the PacketSafari egress approval. Air-gapped customers download the
source through their own vendor relationship and import a bounded signed or
customer-controlled snapshot.

Vendor-direct does **not** mean free commercial use. PacketSafari makes no
purchase and accepts no terms automatically, but the provider may still require
credentials, a subscription, attribution, or an OEM/commercial license. FoxIO
JA4+ product use is a notable commercial-license case; abuse.ch commercial use
may require enhanced access; and Spamhaus's published free redistribution terms
specifically address the original plain-text DROP feed, not PacketSafari's
normalized derivative.

## Customer portal and host workflow

Entitled users open the hosted PacketSafari account portal at:

```text
/app/account/releases
```

It is linked from **Account → On-prem releases**. The page provides signed
release downloads and separates security content into **Included by
PacketSafari**, **Fetch directly from vendor**, **Customer-provided or
imported**, and **Unavailable**.

Connected installations configure sources under the administrative
Intelligence feeds panel, including explicit egress approval. Air-gapped hosts
copy a pack onto approved media and run:

```bash
packetsafari-ops content import --pack ./packetsafari-security-content-<version>.tar.gz
packetsafari-ops content status
```

Content activation does not rewrite old capture results. Rerun Security analysis
explicitly when a capture must use the new content generation.

## Release boundary

The release builder consumes frozen local inputs. It does not download feeds,
grant redistribution approval, accept vendor terms, or publish artifacts as a
side effect. The content pack signature authenticates bytes; it does not grant
license rights. Production signing, image publication, customer delivery, and
deployment remain separately approved release actions.
