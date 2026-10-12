---
name: oci-whatsapp-vm-recovery
description: Diagnose authorized access failures on Oracle Cloud Infrastructure Linux VMs and deploy this repository's WhatsApp engine. Use when SSH credentials are lost, OCI Run Command lacks sudo, serial-console recovery is needed, or the WhatsApp engine must be installed on an OCI VM.
---

# OCI VM Recovery and WhatsApp Engine

## Instructions

### 1. Confirm the target and authority

- Identify the exact tenancy, region, instance OCID, OS, and intended Linux account.
- Confirm that the authenticated OCI identity is authorized to administer the target instance.
- Keep OCI authentication in a dedicated profile. Never print session tokens, API private keys, SSH private keys, passwords, or application secrets.
- Treat an OCI administrator policy as cloud-control-plane access; it does not itself grant root access inside Linux.

### 2. Diagnose without changing the VM

- Inspect instance state, shape and memory, boot settings, VNIC, public IP, subnet, route table, security rules, and Oracle Cloud Agent plugin state.
- Check existing SSH identities by filename and public-key fingerprint only. Never read or display a private key.
- Test SSH with batch mode, strict host-key checking, and a short timeout. Distinguish network/banner failures from `Permission denied (publickey)`.
- Use Run Command first for read-only diagnostics: execution identity, OS, available tools, memory, swap, disk space, and `sudo -n true`.
- Run Command commonly executes as the unprivileged `ocarun` account. Do not assume that IAM policy grants that OS account sudo; do not attempt to exploit the agent or evade Linux permissions.

### 3. Restore access through supported paths

- Prefer the original authorized private key or another documented access path, such as an already configured OCI Bastion or serial-console connection.
- If no valid SSH key is available, explain that a serial console connection does not bypass the Linux login prompt.
- OCI serial-console connections require a supported RSA key in environments that reject Ed25519. Verify the OCI service-host fingerprint against `ssh-keyscan` before trusting it.
- Before a recovery that requires reboot or boot-volume detachment, explain the expected outage and obtain explicit authorization. Back up or otherwise protect the boot volume when practical.
- Use only documented Oracle Linux recovery procedures. If an authorized recovery shell is available, add the user's public key with correct ownership, restrictive permissions, and SELinux labels. Do not enable root SSH, set a shared password, or grant broad `NOPASSWD: ALL`.
- After recovery, test the intended account and its existing least-privilege `sudo` policy. Delete temporary OCI console connections when they are no longer needed.

### 4. Check engine compatibility before installation

- Inspect the repository's `src/whatsapp-engine.js`, `deploy/whatsapp-engine/Dockerfile`, `docker-compose.yml`, and production deployment documentation before choosing an installation method.
- Confirm Node.js, Chromium and its libraries, container runtime if required, persistent WhatsApp session storage, network exposure, and available disk and memory.
- The engine runs Chromium through `whatsapp-web.js`; a small VM with prior out-of-memory events may not be safe for this workload. Recommend increasing memory before starting it rather than treating swap as a reliable substitute.
- Keep the session directory persistent, restrict public ports to the actual caller, and keep internal tokens out of `NEXT_PUBLIC_*`, command output, and logs.
- Do not install packages or change firewall rules until valid administrative access is established.

### 5. Install and verify

- Use the repository's documented deployment path and existing Dockerfile/Compose configuration when its prerequisites are present; avoid creating a parallel engine implementation.
- Make the smallest necessary system and network changes, then verify service status, health response, persistent storage, and application connectivity.
- Report exact blockers when safe installation is not possible. Do not claim success based on an accepted-but-unfinished OCI command.

## Safety boundaries

- Never open SSH to `0.0.0.0/0`; preserve narrowly scoped ingress rules.
- Never bypass IAM, Linux authentication, sudo policy, or the Oracle Cloud Agent's privilege boundary.
- Never disclose or transmit private keys, tokens, passwords, or WhatsApp session data.
- Stop before disruptive recovery when authorization for downtime is unclear.
