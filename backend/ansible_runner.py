import subprocess
import re
import os
import tempfile
import json
from typing import Dict, List, Tuple
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
        self.temp_files = []  # Track temp files for cleanup
    
    def __del__(self):
        """Cleanup temporary key files on destruction"""
        self.cleanup_temp_files()
    
    def cleanup_temp_files(self):
        """Remove all temporary SSH key files"""
        for filepath in self.temp_files:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                print(f"Warning: Failed to cleanup {filepath}: {e}")
        self.temp_files.clear()

    async def _create_dynamic_inventory(self, limit_hosts: List[str] = None) -> str:
        """
        Create a dynamic inventory file with per-host SSH keys
        
        Args:
            limit_hosts: Optional list of hostnames to include
            
        Returns:
            Path to the temporary inventory file
        """
        if not self.db_client:
            # Fallback to static inventory if no database
            return self.inventory_path
        
        try:
            # Fetch all hosts from database
            hosts = await self.db_client.get_all_hosts()
            
            # Build inventory structure
            inventory_data = {
                'all': {
                    'hosts': {},
                    'vars': {
                        'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ControlMaster=no -o ControlPersist=no'
                    }
                }
            }
            
            for host in hosts:
                hostname = host['hostname']
                
                # Skip if limiting and host not in list
                if limit_hosts and hostname not in limit_hosts:
                    continue
                
                host_vars = {
                    'ansible_host': host.get('ip_address') or hostname,
                    'ansible_user': host.get('ssh_user', 'root'),
                    'ansible_port': host.get('ssh_port', 22)
                }
                
                # Handle SSH key if encrypted key exists
                if host.get('ssh_private_key_encrypted'):
                    try:
                        # Decrypt the SSH key
                        decrypted_key = decrypt_credential(host['ssh_private_key_encrypted'])
                        print(f"DEBUG: Decrypted key for {hostname}: {decrypted_key[:50]}... (length: {len(decrypted_key)})")

                        
                        # Write to temporary file
                        key_fd, key_path = tempfile.mkstemp(prefix=f'ansible_key_{hostname}_', suffix='.pem')
                        os.write(key_fd, decrypted_key.encode())
                        os.close(key_fd)
                        os.chmod(key_path, 0o600)  # Set proper permissions
                        
                        # Track for cleanup
                        self.temp_files.append(key_path)
                        print(f"DEBUG: Created temp key file for {hostname}: {key_path}")

                        
                        # Add to host vars
                        host_vars['ansible_ssh_private_key_file'] = key_path
                        
                    except Exception as e:
                        print(f"Warning: Failed to decrypt key for {hostname}: {e}")
                
                # Handle SSH password if exists
                if host.get('ssh_password_encrypted'):
                    try:
                        decrypted_password = decrypt_credential(host['ssh_password_encrypted'])
                        host_vars['ansible_ssh_pass'] = decrypted_password
                        host_vars['ansible_password'] = decrypted_password
                    except Exception as e:
                        print(f"Warning: Failed to decrypt password for {hostname}: {e}")
                
                inventory_data['all']['hosts'][hostname] = host_vars
                # Add reboot and control node flags
                host_vars['allow_auto_reboot'] = host.get('allow_auto_reboot', False)
                host_vars['is_control_node'] = host.get('is_control_node', False)
            
            # Write inventory to temp file
            inv_fd, inv_path = tempfile.mkstemp(prefix='ansible_inventory_', suffix='.json')
            os.write(inv_fd, json.dumps(inventory_data, indent=2).encode())
            os.close(inv_fd)
            self.temp_files.append(inv_path)
            
            return inv_path
            
        except Exception as e:
            print(f"Error creating dynamic inventory: {e}")
            return self.inventory_path

    async def run_check(self, limit_hosts: List[str] = None) -> Tuple[bool, Dict]:
        """
        Run the check playbook and return parsed results
        Returns: (success, results_dict)
        """
        try:
            
            # Create dynamic inventory with decrypted keys
            if self.db_client:
                inventory_path = await self._create_dynamic_inventory(limit_hosts)
            else:
                inventory_path = self.inventory_path
            
            # Run ansible playbook with JSON output
            cmd = [
                "ansible-playbook",
                "-i", inventory_path,
                self.playbook_path,
                "-v"  # Verbose for better parsing
            ]
            # Add limit if specified
            if limit_hosts:
                cmd.extend(["--limit", ",".join(limit_hosts)])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Parse the output
            # DEBUG: Print stderr to see connection errors
            if result.stderr:
                print(f"DEBUG: Ansible stderr:\n{result.stderr}")
            if '192.168.1.50' in result.stdout:
                # Find lines around 192.168.1.50 failure
                lines = result.stdout.split('\n')
                for i, line in enumerate(lines):
                    if '192.168.1.50' in line and ('UNREACHABLE' in line or 'failed' in line):
                        print(f"DEBUG: 192.168.1.50 failure context:")
                        print('\n'.join(lines[max(0,i-5):min(len(lines),i+10)]))
                        break

            hosts_data = self._parse_ansible_output(result.stdout)
            
            # DEBUG: Print what we got
            print(f"DEBUG: Parsed {len(hosts_data)} hosts from Ansible output")
            print(f"DEBUG: Return code: {result.returncode}")
            print(f"DEBUG: Hosts: {list(hosts_data.keys())}")
            
            # Cleanup temp files
            self.cleanup_temp_files()
            
            # Return success=True if we got ANY host data, even if some hosts failed
            return len(hosts_data) > 0 or result.returncode == 0, hosts_data
            
        except subprocess.TimeoutExpired:
            self.cleanup_temp_files()
            return False, {"error": "Ansible playbook timed out"}
        except Exception as e:
            print(f"ERROR in run_check: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            self.cleanup_temp_files()
            return False, {"error": str(e)}
            return False, {"error": str(e)}

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
        try:
            import asyncio
            # Create dynamic inventory with decrypted keys
            if self.db_client:
                inventory_path = await self._create_dynamic_inventory(limit_hosts)
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
            
            # Use Popen for streaming output
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            output_lines = []
            
            # Read output line by line
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                output_lines.append(line)
                
            # Send only important lines to callback
                if progress_callback and line.strip():
                    # Filter for meaningful messages only
                    line_clean = line.strip()
                    
                    # Show TASK headers
                    if line_clean.startswith('TASK ['):
                        await progress_callback(line_clean)
                    # Show changed/failed status
                    elif 'changed:' in line_clean or 'failed:' in line_clean:
                        await progress_callback(line_clean)
                    # Show upgrade summary (e.g., "5 upgraded, 0 newly installed")
                    elif ' upgraded,' in line_clean and ' installed' in line_clean:
                        await progress_callback(f"📦 {line_clean}")
                    # Show reboot messages
                    elif 'Rebooting' in line_clean or 'PLAY RECAP' in line_clean:
                        await progress_callback(line_clean)
            # Wait for process to complete
            process.wait(timeout=1800)
            
            # Cleanup temp files
            self.cleanup_temp_files()
            
            output = ''.join(output_lines)
            
            if process.returncode == 0:
                return True, {"returncode": process.returncode, "output": output}
            else:
                return False, {"error": f"Ansible failed with code {process.returncode}", "output": output}
                
        except subprocess.TimeoutExpired:
            if 'process' in locals():
                process.kill()
            self.cleanup_temp_files()
            return False, {"error": "Ansible patch timed out"}
        except Exception as e:
            print(f"ERROR in run_patch: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            self.cleanup_temp_files()
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
