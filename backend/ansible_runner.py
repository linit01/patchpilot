import subprocess
import sys
import re
import os
import asyncio
import tempfile
import json
from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)
from encryption_utils import decrypt_credential

class AnsibleRunner:
    def __init__(self, playbook_path: str, inventory_path: str, db_client=None):
        """
        Initialize AnsibleRunner with paths and optional database client
        
        Args:
            playbook_path: Path to the Ansible playbook
            inventory_path: Path to the Ansible inventory (used as fallback)
            db_client: DatabaseClient instance for fetching host credentials
        """
        self.playbook_path = playbook_path
        self.inventory_path = inventory_path
        self.db_client = db_client
        # NOTE: No instance-level temp_files list. Each run_check / run_patch
        # call gets its own local list so concurrent or back-to-back runs on this
        # singleton cannot accidentally clean up each other's key files.

    def _cleanup_files(self, file_list: list):
        """Remove a caller-supplied list of temporary files."""
        for filepath in file_list:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                logger.warning(f"Failed to cleanup {filepath}: {e}")
        file_list.clear()
    
    async def _create_dynamic_inventory(self, limit_hosts: List[str] = None) -> tuple:
        """
        Create a dynamic inventory file with per-host SSH keys.

        Returns:
            (inventory_path: str, temp_files: list)
            The caller owns temp_files and must call _cleanup_files(temp_files)
            AFTER the ansible subprocess has fully exited.  This avoids the
            shared-state race where a concurrent run_check could wipe key
            files that a simultaneous run_patch is still using.
        """
        local_temp = []   # owned exclusively by this invocation

        if not self.db_client:
            return self.inventory_path, local_temp

        try:
            hosts = await self.db_client.get_all_hosts()

            # Resolve the default saved SSH key once — used for any host with ssh_key_type='default'
            default_key_content = None
            try:
                async with self.db_client.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT ssh_key_encrypted FROM saved_ssh_keys WHERE is_default = TRUE LIMIT 1"
                    )
                    if row and row['ssh_key_encrypted']:
                        default_key_content = decrypt_credential(row['ssh_key_encrypted'])
                        logger.debug(f"Loaded default saved SSH key (length: {len(default_key_content)})")
            except Exception as e:
                logger.warning(f"Could not load default saved key: {e}")

            inventory_data = {
                'all': {
                    'hosts': {},
                    'vars': {
                        'ansible_ssh_common_args': (
                            '-o StrictHostKeyChecking=no '
                            '-o UserKnownHostsFile=/dev/null '
                            '-o ControlMaster=no '
                            '-o ControlPersist=no'
                        )
                    }
                }
            }

            for host in hosts:
                hostname = host['hostname']

                if limit_hosts and hostname not in limit_hosts:
                    continue

                is_control = host.get('is_control_node', False)
                host_vars = {
                    'ansible_host': host.get('ip_address') or hostname,
                    'ansible_user': host.get('ssh_user', 'root'),
                    'ansible_port': host.get('ssh_port', 22),
                    'allow_auto_reboot': host.get('allow_auto_reboot', False),
                    'is_control_node': is_control,
                }

                logger.debug(f"Host {hostname}: ssh_key_type={host.get('ssh_key_type')}, "
                      f"has_encrypted_key={host.get('ssh_private_key_encrypted') is not None}")

                # Resolve 'default' key_type to the saved default key
                resolved_key_encrypted = host.get('ssh_private_key_encrypted')
                if not resolved_key_encrypted and host.get('ssh_key_type') == 'default' and default_key_content:
                    # Use default key directly (already decrypted above)
                    try:
                        key_fd, key_path = tempfile.mkstemp(
                            prefix=f'ansible_key_{hostname}_', suffix='.pem'
                        )
                        key_data = default_key_content.replace('\r\n', '\n').replace('\r', '\n')
                        if not key_data.endswith('\n'):
                            key_data += '\n'
                        os.write(key_fd, key_data.encode())
                        os.close(key_fd)
                        os.chmod(key_path, 0o600)
                        local_temp.append(key_path)
                        host_vars['ansible_ssh_private_key_file'] = key_path
                        logger.debug(f"Using default saved key for {hostname}")
                    except Exception as e:
                        logger.error(f"Failed to write default key for {hostname}: {e}")

                if resolved_key_encrypted:
                    try:
                        logger.debug(f"Decrypting key for {hostname}...")
                        decrypted_key = decrypt_credential(host['ssh_private_key_encrypted'])
                        logger.debug(f"Decrypted key for {hostname} (length: {len(decrypted_key)})")

                        key_fd, key_path = tempfile.mkstemp(
                            prefix=f'ansible_key_{hostname}_', suffix='.pem'
                        )
                        key_data = decrypted_key.replace('\r\n', '\n').replace('\r', '\n')
                        if not key_data.endswith('\n'):
                            key_data += '\n'
                        os.write(key_fd, key_data.encode())
                        os.close(key_fd)
                        os.chmod(key_path, 0o600)

                        if not os.path.exists(key_path) or os.path.getsize(key_path) == 0:
                            raise RuntimeError(
                                f"Temp key file write failed for {hostname}: {key_path}"
                            )

                        local_temp.append(key_path)
                        logger.debug(f"Created temp key file for {hostname}: {key_path}")
                        host_vars['ansible_ssh_private_key_file'] = key_path

                    except Exception as e:
                        logger.error(f"Failed to decrypt/write key for {hostname}: {e}")

                if host.get('ssh_password_encrypted'):
                    try:
                        decrypted_password = decrypt_credential(host['ssh_password_encrypted'])
                        host_vars['ansible_ssh_pass'] = decrypted_password
                        host_vars['ansible_password'] = decrypted_password
                    except Exception as e:
                        logger.warning(f"Failed to decrypt password for {hostname}: {e}")

                # Windows hosts require PowerShell as the shell type for Ansible
                # over SSH. os_family is set after the first successful host check
                # via gather_facts. For brand-new hosts, the user must set os_type
                # to 'Windows' manually (or we detect it from the connection test).
                os_family = (host.get('os_family') or '').strip()
                os_type = (host.get('os_type') or '').strip()
                if os_family == 'Windows' or os_type.startswith('Microsoft Windows'):
                    host_vars['ansible_shell_type'] = 'powershell'
                    host_vars['ansible_connection'] = 'ssh'
                    host_vars['ansible_become_method'] = 'runas'

                inventory_data['all']['hosts'][hostname] = host_vars

            logger.debug(f"Dynamic inventory for hosts: {list(inventory_data['all']['hosts'].keys())}")

            inv_fd, inv_path = tempfile.mkstemp(prefix='ansible_inventory_', suffix='.json')
            os.write(inv_fd, json.dumps(inventory_data, indent=2).encode())
            os.close(inv_fd)
            local_temp.append(inv_path)

            return inv_path, local_temp

        except Exception as e:
            logger.error(f"Error creating dynamic inventory: {e}")
            self._cleanup_files(local_temp)
            return self.inventory_path, []

    @staticmethod
    def _ansible_check_failure_message(returncode: int, stdout: str, stderr: str, limit: int = 1800) -> str:
        """Human-readable reason when the check playbook exits non-zero with no parsed hosts."""
        parts = [f"ansible-playbook exit {returncode}"]
        tail = (stderr or "").strip() or (stdout or "").strip()
        if tail:
            tail = tail[-limit:] if len(tail) > limit else tail
            one_line = " ".join(tail.splitlines())
            parts.append(one_line)
        else:
            parts.append("no stdout/stderr from ansible-playbook")
        return " — ".join(parts)

    async def run_check(self, limit_hosts: List[str] = None) -> Tuple[bool, Dict]:
        """
        Run the check playbook and return parsed results (non-blocking async subprocess).
        Returns: (success, results_dict)
        """
        run_temp = []   # temp files for THIS invocation only
        try:
            # Create dynamic inventory; temp files are returned, not stored on self
            if self.db_client:
                inventory_path, run_temp = await self._create_dynamic_inventory(limit_hosts)
            else:
                inventory_path = self.inventory_path

            cmd = [
                "ansible-playbook",
                "-i", inventory_path,
                self.playbook_path,
                "-v",
                "--forks", "5",
                "--timeout", "10"
            ]
            if limit_hosts:
                cmd.extend(["--limit", ",".join(limit_hosts)])

            # Pass MAS settings so the check scan also sees App Store updates
            # when mas_enabled=true (same DB lookup as run_patch)
            check_env = os.environ.copy()
            check_env['PYTHONUNBUFFERED'] = '1'
            check_env['ANSIBLE_FORCE_COLOR'] = '0'
            check_env['ANSIBLE_STDOUT_CALLBACK'] = 'default'
            try:
                if self.db_client and self.db_client.pool:
                    async with self.db_client.pool.acquire() as _conn:
                        _rows = await _conn.fetch(
                            "SELECT key, value FROM settings WHERE key IN "
                            "('mas_enabled', 'mas_excluded_ids', 'mas_per_app_timeout', 'mas_timeout_seconds', "
                            "'winget_excluded_ids', 'winupdate_enabled')"
                        )
                        for _r in _rows:
                            if _r['value'] is not None:
                                # Empty string is valid for exclusion lists (no exclusions)
                                check_env[_r['key'].upper()] = _r['value']
            except Exception as _e:
                logger.warning("Could not load settings for check run: %s", _e)

            # Use async subprocess so the event loop stays responsive
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=check_env
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=300
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                self._cleanup_files(run_temp)
                return False, {"error": "Ansible playbook timed out"}

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            # Save last run output for debugging
            try:
                with open('/tmp/ansible_last_run.txt', 'w') as f:
                    f.write("=== STDOUT ===\n")
                    f.write(stdout)
                    f.write("\n=== STDERR ===\n")
                    f.write(stderr)
                    f.write(f"\n=== returncode={process.returncode} ===\n")
            except Exception:
                pass

            if stderr:
                logger.warning(f"ANSIBLE STDERR:\n{stderr}")

            hosts_data = self._parse_ansible_output(stdout)

            logger.debug(f"Parsed {len(hosts_data)} hosts from Ansible output")
            logger.debug(f"Return code: {process.returncode}")
            logger.debug(f"Hosts: {list(hosts_data.keys())}")

            self._cleanup_files(run_temp)
            ok = len(hosts_data) > 0 or process.returncode == 0
            if not ok:
                err = self._ansible_check_failure_message(
                    process.returncode, stdout, stderr
                )
                return False, {"error": err}
            return True, hosts_data

        except Exception as e:
            logger.error(f"Error in run_check: {type(e).__name__}: {str(e)}", exc_info=True)
            self._cleanup_files(run_temp)
            return False, {"error": str(e)}

    async def _send_parsed_task_output(self, line: str, progress_callback):
        """Parse Ansible changed/ok JSON output and send clean formatted messages"""
        import re as _re
        
        # Extract hostname and status from "changed: [host] => {json}" or "ok: [host] => {json}"
        host_match = _re.match(r'(changed|ok|failed):\s*\[([^\]]+)\]\s*=>\s*(.*)', line, _re.DOTALL)
        if not host_match:
            # Simple changed/ok without JSON — send as-is
            await progress_callback(line[:200])
            return
        
        status = host_match.group(1)
        hostname = host_match.group(2)
        json_str = host_match.group(3)
        
        # Try to parse the JSON
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            # Not valid JSON, send a short summary
            if status == 'changed':
                await progress_callback(f"✅ changed: [{hostname}]")
            else:
                await progress_callback(f"{status}: [{hostname}]")
            return
        
        # If no stdout_lines, just send a summary
        stdout_lines = data.get('stdout_lines', [])
        if not stdout_lines:
            if status == 'changed':
                await progress_callback(f"✅ changed: [{hostname}]")
            else:
                await progress_callback(f"{status}: [{hostname}] — no output")
            return
        
        # Parse apt output into structured phases
        phase = 'update'  # apt-get update
        pkg_count = 0
        download_size = ''
        packages_to_upgrade = []
        
        for sline in stdout_lines:
            s = sline.strip()
            if not s:
                continue
            
            # Skip noise: "Reading database ... X%" progress lines
            if s.startswith('(Reading database'):
                continue
            # Skip carriage-return progress lines
            if '\r' in s:
                continue
            
            # Phase: apt-get update repo fetching
            if s.startswith('Hit:') or s.startswith('Get:'):
                continue  # Skip individual repo lines, too noisy
            if s.startswith('Fetched') and 'in' in s:
                await progress_callback(f"📡 [{hostname}] {s}")
                continue
            
            # Phase: Calculating upgrade  
            if s.startswith('Reading package lists'):
                await progress_callback(f"📋 [{hostname}] Reading package lists...")
                continue
            if s.startswith('Building dependency tree'):
                continue
            if s.startswith('Reading state information'):
                continue
            if s.startswith('Calculating upgrade'):
                await progress_callback(f"🔍 [{hostname}] Calculating upgrade...")
                continue
            
            # Package list header
            if s.startswith('The following packages will be upgraded:'):
                phase = 'pkg_list'
                continue
            
            # Capture package names in the list
            if phase == 'pkg_list':
                if _re.match(r'^\d+ upgraded', s):
                    # Summary line like "73 upgraded, 0 newly installed..."
                    phase = 'download'
                    await progress_callback(f"📦 [{hostname}] {s}")
                    continue
                else:
                    # Package name lines — collect them
                    pkgs = [p.strip() for p in s.split() if p.strip()]
                    packages_to_upgrade.extend(pkgs)
                    continue
            
            # Download info
            if s.startswith('Need to get'):
                await progress_callback(f"📥 [{hostname}] {s}")
                continue
            if s.startswith('After this operation'):
                continue
            
            # Individual package downloads (Get:N lines during download)
            if _re.match(r'^Get:\d+', s):
                continue  # Skip individual download lines
            
            if s.startswith('Preconfiguring'):
                continue
            
            # Unpack phase
            if s.startswith('Preparing to unpack'):
                continue  # Skip, too verbose
            if s.startswith('Unpacking '):
                pkg_match = _re.match(r'Unpacking\s+(\S+)\s+\(([^)]+)\)', s)
                if pkg_match:
                    pkg_count += 1
                    pkg_name = pkg_match.group(1)
                    pkg_ver = pkg_match.group(2)
                    total = len(packages_to_upgrade) or '?'
                    await progress_callback(f"📦 [{hostname}] [{pkg_count}/{total}] Unpacking {pkg_name} ({pkg_ver})")
                continue
            
            # Setup phase
            if s.startswith('Setting up '):
                pkg_match = _re.match(r'Setting up\s+(\S+)\s+\(([^)]+)\)', s)
                if pkg_match:
                    pkg_name = pkg_match.group(1)
                    pkg_ver = pkg_match.group(2)
                    await progress_callback(f"✅ [{hostname}] Setting up {pkg_name} ({pkg_ver})")
                continue
            
            # Processing triggers
            if s.startswith('Processing triggers'):
                trigger_match = _re.match(r'Processing triggers for\s+(\S+)', s)
                if trigger_match:
                    await progress_callback(f"⚙️ [{hostname}] Processing triggers for {trigger_match.group(1)}")
                continue
            
            # Config file updates
            if s.startswith('Installing new version of config'):
                continue  # Skip config file noise
            
            # Service status messages
            if 'is a disabled or a static unit' in s:
                continue
            
            # Diversion messages
            if "diversion" in s.lower():
                continue
            
            # initramfs updates
            if 'update-initramfs' in s:
                await progress_callback(f"🔧 [{hostname}] {s}")
                continue
            
            # Reloading messages
            if 'Reloading' in s:
                await progress_callback(f"🔄 [{hostname}] {s}")
                continue
        
        # Send stderr summary if present (service restarts, needrestart info)
        stderr_lines = data.get('stderr_lines', [])
        if stderr_lines:
            for sline in stderr_lines:
                s = sline.strip()
                if not s:
                    continue
                if s.startswith('Running kernel'):
                    await progress_callback(f"🐧 [{hostname}] {s}")
                elif s.startswith('Restarting services'):
                    await progress_callback(f"🔄 [{hostname}] Restarting services...")
                elif s.startswith(' systemctl restart'):
                    svc = s.replace('systemctl restart ', '').strip()
                    await progress_callback(f"   🔄 [{hostname}] {svc}")
                elif 'No containers need' in s:
                    await progress_callback(f"🐳 [{hostname}] {s}")
                # Skip noisy stderr lines like "Service restarts being deferred", user sessions, etc.
        
        # Final summary
        duration = data.get('delta', '')
        if duration:
            await progress_callback(f"⏱️ [{hostname}] Completed in {duration}")

        # ── Winget output parsing ─────────────────────────────────────────
        # If no apt output was detected, check for winget output patterns.
        # Winget stdout_lines contain per-package blocks:
        #   (1/4) Found <Name> [<PackageId>] Version <ver>
        #   ...download progress...
        #   Successfully installed
        #   -or-
        #   Installer failed with exit code: XXXX
        if stdout_lines and not pkg_count:
            _last_found = None
            _winget_total = None
            for sline in stdout_lines:
                s = sline.strip()
                if not s:
                    continue
                # Skip progress bars and spinners
                if _re.match(r'^\s*[-\\|/]\s*$', s) or '\u2588' in s or '\u2592' in s:
                    continue
                if _re.match(r'^\s*$', s):
                    continue

                # "N upgrades available" — extract total
                upgrades_m = _re.match(r'^(\d+)\s+upgrades?\s+available', s)
                if upgrades_m:
                    _winget_total = upgrades_m.group(1)
                    continue

                # "Found <Name> [<PackageId>] Version <ver>"
                found_m = _re.search(r'\((\d+)/(\d+)\)\s+Found\s+(.+?)\s+\[([^\]]+)\]\s+Version\s+([\d][\d\.]*)', s)
                if found_m:
                    _idx = found_m.group(1)
                    _tot = found_m.group(2)
                    _name = found_m.group(3)
                    _pkg_id = found_m.group(4)
                    _ver = found_m.group(5)
                    _last_found = {'idx': _idx, 'total': _tot, 'name': _name, 'id': _pkg_id, 'ver': _ver}
                    await progress_callback(f"📦 [{hostname}] [{_idx}/{_tot}] {_pkg_id} ({_ver})")
                    continue

                # "Successfully installed"
                if 'Successfully installed' in s and _last_found:
                    await progress_callback(f"✅ [{hostname}] {_last_found['id']} ({_last_found['ver']}) installed")
                    _last_found = None
                    continue

                # "Installer failed"
                if 'Installer failed' in s and _last_found:
                    exit_m = _re.search(r'exit code:\s*(\d+)', s)
                    code = exit_m.group(1) if exit_m else '?'
                    await progress_callback(f"❌ [{hostname}] {_last_found['id']} -- failed (exit code {code})")
                    _last_found = None
                    continue

                # "install technology is different"
                if 'install technology is different' in s and _last_found:
                    await progress_callback(f"⚠️ [{hostname}] {_last_found['id']} -- skipped (different install technology)")
                    _last_found = None
                    continue


    async def run_patch(self, limit_hosts: List[str] = None, become_password: str = None, 
                       progress_callback=None) -> Tuple[bool, Dict]:
        """
        Run the patch playbook with apply-updates tag
        Args:
            limit_hosts: List of hostnames to limit patching to
            become_password: Sudo password for the operation
            progress_callback: Async function to call with progress updates
        Returns: (success, results_dict)
        """
        patch_temp = []  # hoisted so except/finally can always call _cleanup_files
        try:
            import asyncio
            # Create dynamic inventory with decrypted keys
            if self.db_client:
                inventory_path, patch_temp = await self._create_dynamic_inventory(limit_hosts)
            else:
                inventory_path = self.inventory_path

            cmd = [
                "ansible-playbook",
                "-i", inventory_path,
                self.playbook_path,
                "--tags", "apply-updates",
                "-v",
                "--forks", "1",
                "--timeout", "10"
            ]
            
            # Add limit if specified
            if limit_hosts:
                cmd.extend(["--limit", ",".join(limit_hosts)])
            
            # Add become password if specified via extra-vars.
            # MUST use JSON format — raw key=value is parsed as YAML by Ansible,
            # which silently corrupts passwords containing special characters
            # (!, #, :, {, }, @, etc.) causing become auth to fail.
            if become_password:
                import json as _json
                cmd.extend(["--extra-vars", _json.dumps({"ansible_become_password": become_password})])
            
            # Use async subprocess for non-blocking streaming output
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            env['ANSIBLE_FORCE_COLOR'] = '0'
            env['ANSIBLE_STDOUT_CALLBACK'] = 'default'

            # ── macOS / App Store settings ────────────────────────────────
            # Load mas config from DB settings (user-configurable via UI).
            # Falls back to env vars / .env defaults.
            # NOTE: mas_excluded_ids is checked separately — an empty string
            # is a valid intentional value (user cleared all exclusions) and
            # must override the env/default rather than being skipped.
            _mas_excluded_from_db = None
            try:
                if self.db_client and self.db_client.pool:
                    async with self.db_client.pool.acquire() as _conn:
                        _rows = await _conn.fetch(
                            "SELECT key, value FROM settings WHERE key IN "
                            "('macos_system_updates_enabled', "
                            "'mas_enabled', 'mas_excluded_ids', 'mas_per_app_timeout', 'mas_timeout_seconds', "
                            "'winupdate_enabled')"
                        )
                        for _r in _rows:
                            if _r['key'] == 'macos_system_updates_enabled' and _r['value']:
                                env['MACOS_SYSTEM_UPDATES_ENABLED'] = _r['value']
                            elif _r['key'] == 'mas_enabled' and _r['value']:
                                env['MAS_ENABLED'] = _r['value']
                            elif _r['key'] == 'mas_excluded_ids' and _r['value'] is not None:
                                # Store separately — empty string is valid (no exclusions)
                                _mas_excluded_from_db = _r['value']
                            elif _r['key'] == 'mas_per_app_timeout' and _r['value']:
                                env['MAS_PER_APP_TIMEOUT'] = _r['value']
                            elif _r['key'] == 'mas_timeout_seconds' and _r['value']:
                                env['MAS_TIMEOUT_SECONDS'] = _r['value']
            except Exception as _mas_e:
                logger.warning("Could not load mas settings from DB (non-fatal): %s", _mas_e)
            # Explicit env overrides always win — EXCEPT mas_excluded_ids where
            # an empty DB value means "user intentionally cleared all exclusions"
            if os.getenv('MACOS_SYSTEM_UPDATES_ENABLED') and 'MACOS_SYSTEM_UPDATES_ENABLED' not in env:
                env['MACOS_SYSTEM_UPDATES_ENABLED'] = os.getenv('MACOS_SYSTEM_UPDATES_ENABLED', 'false')
            if os.getenv('MAS_ENABLED') and 'MAS_ENABLED' not in env:
                env['MAS_ENABLED'] = os.getenv('MAS_ENABLED', 'false')
            if _mas_excluded_from_db is not None:
                # DB value takes precedence — even empty string (no exclusions)
                env['MAS_EXCLUDED_IDS'] = _mas_excluded_from_db
            elif os.getenv('MAS_EXCLUDED_IDS') and 'MAS_EXCLUDED_IDS' not in env:
                env['MAS_EXCLUDED_IDS'] = os.getenv('MAS_EXCLUDED_IDS', '')
            if os.getenv('MAS_PER_APP_TIMEOUT') and 'MAS_PER_APP_TIMEOUT' not in env:
                env['MAS_PER_APP_TIMEOUT'] = os.getenv('MAS_PER_APP_TIMEOUT', '600')
            if os.getenv('MAS_TIMEOUT_SECONDS') and 'MAS_TIMEOUT_SECONDS' not in env:
                env['MAS_TIMEOUT_SECONDS'] = os.getenv('MAS_TIMEOUT_SECONDS', '7200')

            # ── Windows / winget settings ──────────────────────────────────
            _winget_excluded_from_db = None
            try:
                if self.db_client and self.db_client.pool:
                    async with self.db_client.pool.acquire() as _conn:
                        _row = await _conn.fetchrow(
                            "SELECT value FROM settings WHERE key = 'winget_excluded_ids'"
                        )
                        if _row and _row['value'] is not None:
                            _winget_excluded_from_db = _row['value']
            except Exception as _we:
                logger.warning("Could not load winget settings from DB (non-fatal): %s", _we)
            if _winget_excluded_from_db is not None:
                env['WINGET_EXCLUDED_IDS'] = _winget_excluded_from_db
            elif os.getenv('WINGET_EXCLUDED_IDS'):
                env['WINGET_EXCLUDED_IDS'] = os.getenv('WINGET_EXCLUDED_IDS', '')

            # ── Windows Update (PSWindowsUpdate) settings ──────────────────
            _winupdate_from_db = None
            try:
                if self.db_client and self.db_client.pool:
                    async with self.db_client.pool.acquire() as _conn:
                        _row = await _conn.fetchrow(
                            "SELECT value FROM settings WHERE key = 'winupdate_enabled'"
                        )
                        if _row and _row['value'] is not None:
                            _winupdate_from_db = _row['value']
            except Exception as _wue:
                logger.warning("Could not load winupdate settings from DB (non-fatal): %s", _wue)
            if _winupdate_from_db is not None:
                env['WINUPDATE_ENABLED'] = _winupdate_from_db
            elif os.getenv('WINUPDATE_ENABLED'):
                env['WINUPDATE_ENABLED'] = os.getenv('WINUPDATE_ENABLED', 'false')

            # Overall runner timeout = larger of 30 min or mas_timeout + 5 min buffer
            try:
                _mas_secs = int(env.get('MAS_TIMEOUT_SECONDS', '7200'))
            except (ValueError, TypeError):
                _mas_secs = 7200
            _runner_timeout = max(1800, _mas_secs + 300)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                limit=4 * 1024 * 1024  # 4MB buffer - apt output can be huge
            )

            output_lines = []
            line_count = 0

            # Read output line by line - async, so the event loop stays free
            # to flush WebSocket messages between lines
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode('utf-8', errors='replace')
                output_lines.append(line)
                logger.debug(f"PATCH LINE: {line.strip()[:200]}")
                sys.stdout.flush()
                line_count += 1
 
                if progress_callback and line.strip():
                    line_clean = line.strip()

                    # Show TASK headers
                    if line_clean.startswith('TASK ['):
                        await progress_callback(line_clean)
                    # Parse changed/ok lines that contain JSON apt output
                    elif ('changed:' in line_clean or 'ok:' in line_clean) and '=>' in line_clean:
                        await self._send_parsed_task_output(line_clean, progress_callback)
                    # Show skipping/unreachable
                    elif 'skipping:' in line_clean:
                        await progress_callback(line_clean)
                    elif 'unreachable:' in line_clean:
                        await progress_callback(f"⚠️ {line_clean}")
                    # Show reboot messages and play recap
                    elif 'Rebooting' in line_clean or 'PLAY RECAP' in line_clean:
                        await progress_callback(line_clean)
                    # Show fatal errors
                    elif 'fatal:' in line_clean or 'FAILED!' in line_clean:
                        await progress_callback(f"❌ {line_clean}")
                    # ── Async polling heartbeats (mas / winget / long-running tasks) ──
                    # Ansible emits "ASYNC POLL on host: jid=... finished=0" every
                    # poll seconds. Without forwarding these the UI appears frozen
                    # because the shell script produces no output during downloads.
                    elif 'ASYNC POLL' in line_clean:
                        import re as _re
                        _host = _re.search(r'ASYNC POLL on ([^:]+)', line_clean)
                        _fin  = _re.search(r'finished=(\d+)', line_clean)
                        _host_s = _host.group(1).strip() if _host else '?'
                        _fin_s  = _fin.group(1) if _fin else '0'
                        if _fin_s == '0':
                            await progress_callback(f"⏳ [{_host_s}] Package updates in progress...")
                        # finished=1 is followed immediately by ASYNC OK/FAILED — skip
                    elif 'ASYNC OK' in line_clean:
                        await progress_callback(f"✅ Async task complete")
                    elif 'ASYNC FAILED' in line_clean:
                        await progress_callback(f"❌ Async task failed")
                    # mas result lines emitted by the "Show App Store update results" debug task.
                    # These appear AFTER the async task completes, in the debug output.
                    # Format: "msg": "START [id] name | DONE [id] name | SUMMARY: ..."
                    elif any(k in line_clean for k in ('START [', 'DONE [', 'SKIP [', 'TIMEOUT [', 'ERROR [', 'SUMMARY:', 'No App Store')):
                        await progress_callback(f"🍎 {line_clean}")

            # Wait for process to complete
            logger.debug(f"Patch output: {line_count} lines read")
            await asyncio.wait_for(process.wait(), timeout=_runner_timeout)
            
            # Cleanup temp files owned by this patch run only
            self._cleanup_files(patch_temp)
            
            output = ''.join(output_lines)
            
            if process.returncode == 0:
                return True, {"returncode": process.returncode, "output": output}
            else:
                return False, {"error": f"Ansible failed with code {process.returncode}", "output": output}
                
        except asyncio.TimeoutError:
            if 'process' in locals():
                process.kill()
            self._cleanup_files(patch_temp)
            return False, {"error": "Ansible patch timed out"}
        except Exception as e:
            logger.error(f"Error in run_patch: {type(e).__name__}: {str(e)}", exc_info=True)
            self._cleanup_files(patch_temp)
            return False, {"error": str(e)}

    def _parse_ansible_output(self, output: str) -> Dict:
        """
        Parse Ansible playbook output to extract host information
        Returns dict: {hostname: {status, total_updates, update_details, os_family, os_type, ip_address}}
        """
        hosts_data = {}
        current_host = None
        in_recap = False
        
        # Parse output line by line
        lines = output.split('\n')
        for i, line in enumerate(lines):
            # Detect PLAY RECAP section
            if 'PLAY RECAP' in line:
                in_recap = True
                continue
            
            # Parse PLAY RECAP to get host status
            elif in_recap and line.strip() and not line.startswith('PLAY RECAP'):
                # Format: "hostname : ok=X changed=Y unreachable=Z failed=W"
                match = re.match(r'^([^\s:]+)\s*:\s*ok=(\d+).*unreachable=(\d+).*failed=(\d+)', line)
                if match:
                    hostname = match.group(1)
                    unreachable = int(match.group(3))
                    failed = int(match.group(4))
                    
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    
                    # Determine status from recap.
                    # Only set negative status here — positive status (up-to-date /
                    # updates-available) is determined later from actual task output.
                    # If recap shows unreachable=0 and failed=0, explicitly mark the
                    # host as reachable so a previous stale "failed" status in the DB
                    # is always overwritten by a clean run.
                    if unreachable > 0:
                        hosts_data[hostname]['status'] = 'unreachable'
                        print(f"[PARSER] {hostname}: RECAP unreachable={unreachable} → status=unreachable")
                    elif failed > 0:
                        hosts_data[hostname]['status'] = 'failed'
                        print(f"[PARSER] {hostname}: RECAP failed={failed} → status=failed")
                    else:
                        # Clean run — mark reachable; will be refined to up-to-date or
                        # updates-available once task output is parsed below.
                        # NOTE: setdefault is intentional — HOSTSTATUS (emitted by the
                        # playbook) takes priority over RECAP because ignore_unreachable
                        # masks genuine connection failures in the RECAP counters.
                        hosts_data[hostname].setdefault('status', 'up-to-date')
                        print(f"[PARSER] {hostname}: RECAP ok={match.group(2)} failed=0 unreachable=0 → reachable")
            
            # Capture OS family
            if '"ansible_os_family"' in line or 'ansible_os_family' in line:
                match = re.search(r'"?ansible_os_family"?\s*[:=]\s*"?([^",\s]+)"?', line)
                if match and current_host:
                    hosts_data[current_host]['os_family'] = match.group(1)
            
            # Capture distribution (OS type)
            if '"ansible_distribution"' in line or 'ansible_distribution' in line:
                match = re.search(r'"?ansible_distribution"?\s*[:=]\s*"?([^",\s]+)"?', line)
                if match and current_host:
                    hosts_data[current_host]['os_type'] = match.group(1)
            
            # Capture IP address
            if '"ansible_default_ipv4"' in line:
                # Look ahead for the address field
                for j in range(i, min(i+5, len(lines))):
                    ip_match = re.search(r'"address":\s*"([0-9.]+)"', lines[j])
                    if ip_match and current_host:
                        hosts_data[current_host]['ip_address'] = ip_match.group(1)
                        break

            # Look for host info (HOSTINFO: hostname | IP: x.x.x.x | OS: Ubuntu | Family: Debian)
            if 'HOSTINFO:' in line:
                match = re.search(r'HOSTINFO:\s*([^\s|"]+)\s*\|\s*IP:\s*([^\s|"]+)\s*\|\s*OS:\s*([^|"]+?)\s*\|\s*Family:\s*([^\s|"]+)', line)
                if match:
                    hostname = match.group(1)
                    ip_addr = match.group(2)
                    os_type = match.group(3).strip()
                    os_family = match.group(4)
                    
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    
                    if ip_addr != 'N/A':
                        hosts_data[hostname]['ip_address'] = ip_addr
                    elif re.match(r'^\d+\.\d+\.\d+\.\d+$', hostname):
                        # Hostname is already an IPv4 address — use it
                        hosts_data[hostname]['ip_address'] = hostname
                    hosts_data[hostname]['os_type'] = os_type
                    hosts_data[hostname]['os_family'] = os_family
                    current_host = hostname

            # Look for explicit unreachable marker emitted before meta: end_host
            # Format: "HOSTSTATUS: hostname | unreachable"
            if 'HOSTSTATUS:' in line:
                match = re.search(r'HOSTSTATUS:\s*([^\s|"]+)\s*\|\s*(\w+)', line)
                if match:
                    hostname = match.group(1)
                    explicit_status = match.group(2)
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    hosts_data[hostname]['status'] = explicit_status
                    hosts_data[hostname]['total_updates'] = 0
                    print(f"[PARSER] {hostname}: HOSTSTATUS={explicit_status}")
                    current_host = hostname

            # Look for "Show update status" messages with package counts
            if 'msg' in line and 'updates available' in line.lower():
                # Matches: "hostname:  65 apt updates available"
                #           "hostname:  2 App Store updates available"
                #           "hostname:  3 brew updates available"
                #           "hostname:  macOS system updates available"  (no count)
                #           "hostname:  App Store updates available"     (no count)
                match = re.search(r'"msg":\s*"[^\d]*?([^\s:|]+)\s*[:|]\s*(\d+)\s+(?:\w+\s+){0,3}updates?\s+available', line)
                macos_qual_match = re.search(r'"msg":\s*"([^\s|":\\n]+)[:\\n]+\s*(?:macOS\s+system|App\s+Store)\s+updates?\s+available', line)

                if match:
                    hostname = match.group(1)
                    count = int(match.group(2))
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    hosts_data[hostname]['total_updates'] = count
                    hosts_data[hostname]['status'] = 'updates-available' if count > 0 else 'up-to-date'
                    current_host = hostname
                elif macos_qual_match:
                    # qualitative macOS message without a count — status is known,
                    # exact count will be set during final reconciliation from PACKAGE: lines
                    hostname = macos_qual_match.group(1)
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    hosts_data[hostname].setdefault('total_updates', 0)
                    hosts_data[hostname]['status'] = 'updates-available'
                    current_host = hostname
            
            # Look for package details (PACKAGE: hostname | package_name)
            if 'PACKAGE:' in line:
                match = re.search(r'PACKAGE:\s*([^\s|]+)\s*\|\s*(.+)', line)
                if match:
                    hostname = match.group(1)
                    package_data = match.group(2).strip().strip('"')

                    # Skip known noise lines from softwareupdate -l output
                    # that should never have been emitted as PACKAGE lines.
                    _noise = package_data.lower().strip()
                    if (_noise.startswith('software update tool') or
                        _noise.startswith('finding available software') or
                        _noise.startswith('no new software available') or
                        _noise.startswith('software update found') or
                        _noise == ''):
                        continue
                    
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {'status': 'updates-available', 'total_updates': 0, 'update_details': []}
                    if 'update_details' not in hosts_data[hostname]:
                        hosts_data[hostname]['update_details'] = []
                    
                    # Parse package information
                    # Debian format: "package_name (current_version) [available_version]"
                    deb_match = re.search(r'^([\w\-\.]+)(?:/[\w\-\.,]+)?\s+(\S+)\s+(?:amd64|arm64|all|i386)\s+\[upgradable from:\s+(\S+)\]', package_data)
                    # Homebrew format: "package_name (current_version) < available_version"
                    brew_match = re.search(r'^([\w\-\.@]+)\s+\(([\d\._]+)\)\s+<\s+([\d\._]+)', package_data)
                    
                    if deb_match:
                        hosts_data[hostname]['update_details'].append({
                            'package_name': deb_match.group(1),
                            'current_version': deb_match.group(3),
                            'available_version': deb_match.group(2),
                            'update_type': 'apt'
                        })
                    elif brew_match:
                        hosts_data[hostname]['update_details'].append({
                            'package_name': brew_match.group(1),
                            'current_version': brew_match.group(2),
                            'available_version': brew_match.group(3),
                            'update_type': 'brew'
                        })
                    elif hosts_data.get(hostname, {}).get('os_family') == 'Darwin' and re.match(r'^[\w\-\.@/]+$', package_data):
                        # Bare brew package name (no version info — brew outdated without --verbose)
                        hosts_data[hostname]['update_details'].append({
                            'package_name': package_data.strip(),
                            'current_version': 'installed',
                            'available_version': 'available',
                            'update_type': 'brew'
                        })
                    else:
                        # macOS system updates format: "* Label: macOS Sequoia 15.3-24D2068"
                        # or "   Title: macOS Sequoia 15.3, Version: 15.3, Size: 7331569K"
                        macos_match = re.search(r'\*\s+Label:\s+(.+?)-[\w\.]+', package_data)
                        if macos_match:
                            hosts_data[hostname]['update_details'].append({
                                'package_name': macos_match.group(1).strip(),
                                'current_version': 'installed',
                                'available_version': 'update available',
                                'update_type': 'macos-system'
                            })
                        else:
                            # App Store (mas) format: "1234567890 AppName (1.0 -> 2.0)"
                            mas_match = re.search(r'^\d+\s+(.+?)\s+\(([\d\.]+)\s+->\s+([\d\.]+)\)', package_data)
                            if mas_match:
                                hosts_data[hostname]['update_details'].append({
                                    'package_name': mas_match.group(1),
                                    'current_version': mas_match.group(2),
                                    'available_version': mas_match.group(3),
                                    'update_type': 'mas'
                                })
                            else:
                                # Winget format: "AppName (Package.Id) 1.0.0 -> 2.0.0"
                                # Note: winget truncates long names with ellipsis which
                                # gets mangled into garbage Unicode over SSH, so we use
                                # a permissive approach: match "ver -> ver" at the end,
                                # then extract the dotted package ID separately.
                                winget_ver = re.search(r'([\d][\d\.]+)\s+->\s+([\d][\d\.]+)\s*$', package_data)
                                if winget_ver:
                                    # Extract dotted package ID (e.g. Microsoft.Teams)
                                    winget_id = re.search(r'[\s\(]([A-Za-z][\w\-]*\.[A-Za-z][\w\.\-\+]*)', package_data)
                                    pkg_name = winget_id.group(1) if winget_id else package_data.split('(')[0].strip()[:50]
                                    hosts_data[hostname]['update_details'].append({
                                        'package_name': pkg_name,
                                        'current_version': winget_ver.group(1),
                                        'available_version': winget_ver.group(2),
                                        'update_type': 'winget'
                                    })
                                else:
                                    # Windows Update (PSWindowsUpdate) format:
                                    #   New: "Title (KB#) installed -> available"
                                    #   Old: "KB# size Title"  or  "KB# 56GB 2026-03 Security Update..."
                                    winupdate_kb = re.search(r'(KB\d+)', package_data)
                                    if winupdate_kb:
                                        kb = winupdate_kb.group(1)
                                        # Strip trailing "installed -> available" (new format)
                                        title = re.sub(r'\s+installed\s*->\s*available\s*$', '', package_data)
                                        # Strip the KB token itself (and surrounding parens)
                                        title = re.sub(r'\(?' + re.escape(kb) + r'\)?', '', title)
                                        # Strip size tokens like "56GB", "1GB", "920019KiB"
                                        title = re.sub(r'\b\d+(?:\.\d+)?\s*[KMGT]i?B\b', '', title, flags=re.IGNORECASE)
                                        # Strip leading noise (line numbers, dashes, whitespace)
                                        title = re.sub(r'^\s*[\d\-\s]+', '', title).strip(' ,')
                                        title = re.sub(r'\s{2,}', ' ', title).strip()
                                        # Strip empty parens left over
                                        title = re.sub(r'\(\s*\)', '', title).strip()
                                        if not title:
                                            title = kb
                                        hosts_data[hostname]['update_details'].append({
                                            'package_name': f"{title} ({kb})" if kb not in title else title,
                                            'current_version': 'installed',
                                            'available_version': 'available',
                                            'update_type': 'winupdate'
                                        })
                                    # macOS softwareupdate Title line (duplicate of * Label line):
                                    #   "Title: macOS Tahoe 26.4, Version: 26.4, Size: ..."
                                    #   May arrive with leading tab or literal "\t" from Ansible
                                    elif re.match(r'^(?:\\t|\t)?\s*Title:\s+', package_data):
                                        # Skip — the * Label line already emitted this update
                                        pass
                                    else:
                                        # Unknown format -- store raw line as a generic package
                                        hosts_data[hostname]['update_details'].append({
                                            'package_name': package_data[:80],
                                            'current_version': 'unknown',
                                            'available_version': 'available',
                                            'update_type': 'unknown'
                                        })

        # Look for reboot required status
            if 'Check if reboot required' in line:
                # Look ahead for the result
                for j in range(i, min(i+10, len(lines))):
                    if '"exists": true' in lines[j] and current_host:
                        if current_host not in hosts_data:
                            hosts_data[current_host] = {}
                        hosts_data[current_host]['reboot_required'] = True
                        break
                    elif '"exists": false' in lines[j] and current_host:
                        if current_host not in hosts_data:
                            hosts_data[current_host] = {}
                        hosts_data[current_host]['reboot_required'] = False
                        break

            # Windows reboot detection — check for REBOOT_REQUIRED in stdout
            if 'REBOOT_REQUIRED' in line and 'needs a reboot' in line:
                # Extract hostname from the debug message
                reboot_m = re.search(r'REBOOT_REQUIRED:\s*(\S+)\s+needs a reboot', line)
                if reboot_m:
                    rhost = reboot_m.group(1)
                    if rhost not in hosts_data:
                        hosts_data[rhost] = {}
                    hosts_data[rhost]['reboot_required'] = True
            elif 'Set Windows reboot fact' in line or 'win_reboot_needed' in line:
                # Check for the set_fact result
                for j in range(i, min(i+5, len(lines))):
                    if 'REBOOT_REQUIRED' in lines[j] and current_host:
                        if current_host not in hosts_data:
                            hosts_data[current_host] = {}
                        hosts_data[current_host]['reboot_required'] = True
                        break

        # Reconcile totals — always trust update_details count over parsed numbers.
        # The "Show update status" debug message only carries numeric counts for
        # apt/brew; macOS system and mas updates don't emit a count there, so
        # total_updates may be 0 even when update_details is populated.
        for hostname in hosts_data:
            detail_count = len(hosts_data[hostname].get('update_details', []))
            if detail_count > 0:
                # Packages on disk are ground truth
                hosts_data[hostname]['total_updates'] = detail_count
                hosts_data[hostname]['status'] = 'updates-available'
            else:
                # No parseable package details — set total_updates to 0 to stay
                # consistent with the packages table (which will have 0 rows).
                # If the status message claimed updates but PACKAGE: lines were
                # not parseable, log a warning so the user can investigate.
                prev_total = hosts_data[hostname].get('total_updates', 0)
                if prev_total > 0:
                    print(f"[PARSER] WARNING: {hostname} status message claimed "
                          f"{prev_total} updates but 0 package details were parsed "
                          f"— resetting total_updates to 0 to prevent dashboard/details mismatch")
                hosts_data[hostname]['total_updates'] = 0
                # Also reset status to match — unless it was set to a negative
                # status (unreachable/failed) by the RECAP or HOSTSTATUS parser
                cur_status = hosts_data[hostname].get('status', '')
                if cur_status not in ('unreachable', 'failed'):
                    if cur_status == 'updates-available':
                        print(f"[PARSER] WARNING: {hostname} status was 'updates-available' "
                              f"but 0 packages parsed — resetting to 'up-to-date'")
                    hosts_data[hostname]['status'] = 'up-to-date'

            if 'status' not in hosts_data[hostname]:
                if hosts_data[hostname].get('total_updates', 0) > 0:
                    hosts_data[hostname]['status'] = 'updates-available'
                else:
                    hosts_data[hostname]['status'] = 'up-to-date'

        return hosts_data
