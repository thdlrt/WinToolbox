Public Bambu printer trust certificates, retrieved from the ha-bambulab project:
https://github.com/greghesp/ha-bambulab/tree/main/custom_components/bambu_lab/pybambu/certs

These are CA/public certificates, not private keys. Local MQTT verifies the
certificate chain; hostname matching is disabled because a printer's certificate
identifies the device rather than its current LAN IP. No TLS verification is
disabled for weather, Codex or other network clients.
