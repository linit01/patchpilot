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
                print(f"Warning: Failed to cleanup {filepath}: {e}")
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

                host_vars = {
                    'ansible_host': host.get('ip_address') or hostname,
                    'ansible_user': host.get('ssh_user', 'root'),
                    'ansible_port': host.get('ssh_port', 22),
                    'allow_auto_reboot': host.get('allow_auto_reboot', False),
                    'is_control_node': host.get('is_control_node', False),
                }

                print(f"DEBUG Host {hostname}: ssh_key_type={host.get('ssh_key_type')}, "
                      f"has_encrypted_key={host.get('ssh_private_key_encrypted') is not None}")

                if host.get('ssh_private_key_encrypted'):
                    try:
                        print(f"DEBUG: Decrypting key for {hostname}...")
                        decrypted_key = decrypt_credential(host['ssh_private_key_encrypted'])
                        print(f"Decrypted key for {hostname}: {decrypted_key[:50]}... "
                              f"(length: {len(decrypted_key)})")

                        key_fd, key_path = tempfile.mkstemp(
                            prefix=f'ansible_key_{hostname}_', suffix='.pem'
                        )
                        os.write(key_fd, decrypted_key.encode())
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
                        print(f"ERROR: Failed to decrypt/write key for {hostname}: {e}")

                if host.get('ssh_password_encrypted'):
                    try:
                        decrypted_password = decrypt_credential(host['ssh_password_encrypted'])
                        host_vars['ansible_ssh_pass'] = decrypted_password
                        host_vars['ansible_password'] = decrypted_password
                    except Exception as e:
                        print(f"Warning: Failed to decrypt password for {hostname}: {e}")

                inventory_data['all']['hosts'][hostname] = host_vars

            print(f"DEBUG INVENTORY for hosts: {list(inventory_data['all']['hosts'].keys())}")

            inv_fd, inv_path = tempfile.mkstemp(prefix='ansible_inventory_', suffix='.json')
            os.write(inv_fd, json.dumps(inventory_data, indent=2).encode())
            os.close(inv_fd)
            local_temp.append(inv_path)

            return inv_path, local_temp

        except Exception as e:
            print(f"Error creating dynamic inventory: {e}")
            self._cleanup_files(local_temp)
            return self.inventory_path, []

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
                "-v"
            ]
            if limit_hosts:
                cmd.extend(["--limit", ",".join(limit_hosts)])

            # Use async subprocess so the event loop stays responsive
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
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
                    f.write(stdout)
            except Exception:
                pass

            if stderr:
                print(f"ANSIBLE STDERR:\n{stderr}")

            hosts_data = self._parse_ansible_output(stdout)

            logger.debug(f"Parsed {len(hosts_data)} hosts from Ansible output")
            logger.debug(f"Return code: {process.returncode}")
            logger.debug(f"Hosts: {list(hosts_data.keys())}")

            self._cleanup_files(run_temp)
            return len(hosts_data) > 0 or process.returncode == 0, hosts_data

        except Exception as e:
            print(f"ERROR in run_check: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
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
                "-v"
            ]
            
            # Add limit if specified
            if limit_hosts:
                cmd.extend(["--limit", ",".join(limit_hosts)])
            
            # Add become password if specified via extra-vars
            if become_password:
                cmd.extend(["--extra-vars", f"ansible_become_password={become_password}"])
            
            # Use async subprocess for non-blocking streaming output
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            env['ANSIBLE_FORCE_COLOR'] = '0'
            env['ANSIBLE_STDOUT_CALLBACK'] = 'default'

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
                print(f"DEBUG LINE: {line.strip()[:200]}")
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

            # Wait for process to complete
            print(f"TOTAL LINES READ: {line_count}")
            await asyncio.wait_for(process.wait(), timeout=1800)
            
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
            print(f"ERROR in run_patch: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
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
                    
                    # Determine status from recap
                    if unreachable > 0:
                        hosts_data[hostname]['status'] = 'unreachable'
                    elif failed > 0:
                        hosts_data[hostname]['status'] = 'failed'
            
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
                match = re.search(r'HOSTINFO:\s*([^\s|"]+)\s*\|\s*IP:\s*([^\s|"]+)\s*\|\s*OS:\s*([^\s|"]+)\s*\|\s*Family:\s*([^\s|"]+)', line)
                if match:
                    hostname = match.group(1)
                    ip_addr = match.group(2)
                    os_type = match.group(3)
                    os_family = match.group(4)
                    
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    
                    if ip_addr != 'N/A':
                        hosts_data[hostname]['ip_address'] = ip_addr
                    hosts_data[hostname]['os_type'] = os_type
                    hosts_data[hostname]['os_family'] = os_family
                    current_host = hostname

            # Look for "Show update status" messages with package counts
            if 'msg' in line and 'updates available' in line.lower():
                # Extract hostname and count
                # Format: "msg": "10.0.1.104 | 65 updates available"
                match = re.search(r'"msg":\s*"[^\d]*?([^\s:|]+)\s*[:|]\s*(\d+)\s+(?:\w+\s+)?updates?\s+available', line)

                if match:
                    hostname = match.group(1)
                    count = int(match.group(2))
                    if hostname not in hosts_data:
                        hosts_data[hostname] = {}
                    hosts_data[hostname]['total_updates'] = count
                    hosts_data[hostname]['status'] = 'updates-available' if count > 0 else 'up-to-date'
                    current_host = hostname
            
            # Look for package details (PACKAGE: hostname | package_name)
            if 'PACKAGE:' in line:
                match = re.search(r'PACKAGE:\s*([^\s|]+)\s*\|\s*(.+)', line)
                if match:
                    hostname = match.group(1)
                    package_data = match.group(2).strip()
                    
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

        # Set defaults for any hosts that didn't get update counts
        for hostname in hosts_data:
            if 'total_updates' not in hosts_data[hostname]:
                # Count from update_details if available
                if 'update_details' in hosts_data[hostname]:
                    hosts_data[hostname]['total_updates'] = len(hosts_data[hostname]['update_details'])
                else:
                    hosts_data[hostname]['total_updates'] = 0
            
            if 'status' not in hosts_data[hostname]:
                if hosts_data[hostname]['total_updates'] > 0:
                    hosts_data[hostname]['status'] = 'updates-available'
                else:
                    hosts_data[hostname]['status'] = 'up-to-date'
        
        return hosts_data
