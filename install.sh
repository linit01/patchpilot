#!/bin/bash
set -e

# PatchPilot Installer
# Automated system update management for your infrastructure

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
NC='\033[0m' # No Color

# Banner
print_banner() {
    echo -e "${PURPLE}"
    cat << "EOF"
    ____        __       __    ____  _ __      __ 
   / __ \____ _/ /______/ /_  / __ \(_) /___  / /_
  / /_/ / __ `/ __/ ___/ __ \/ /_/ / / / __ \/ __/
 / ____/ /_/ / /_/ /__/ / / / ____/ / / /_/ / /_  
/_/    \__,_/\__/\___/_/ /_/_/   /_/_/\____/\__/  
                                                    
EOF
    echo -e "${NC}"
    echo -e "${BLUE}System Update Management Made Easy${NC}"
    echo ""
}

# Helper functions
print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}!${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_step() {
    echo ""
    echo -e "${PURPLE}▸${NC} $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Check if running as root (we don't want this)
check_root() {
    if [ "$EUID" -eq 0 ]; then
        print_error "Please don't run this installer as root"
        echo "Run as your normal user: ./install.sh"
        exit 1
    fi
}

# Check prerequisites
check_prerequisites() {
    print_step "Checking prerequisites"
    
    local missing_deps=()
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        missing_deps+=("docker")
        print_error "Docker is not installed"
    else
        print_success "Docker is installed"
    fi
    
    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        missing_deps+=("docker-compose")
        print_error "Docker Compose is not installed"
    else
        print_success "Docker Compose is available"
        if docker compose version &> /dev/null; then
            DOCKER_COMPOSE_CMD="docker compose"
        else
            DOCKER_COMPOSE_CMD="docker-compose"
        fi
    fi
    
    # Check Ansible
    if ! command -v ansible &> /dev/null; then
        print_warning "Ansible not found locally (not critical - will run in container)"
    else
        print_success "Ansible is installed"
    fi
    
    if [ ${#missing_deps[@]} -gt 0 ]; then
        echo ""
        print_error "Missing required dependencies: ${missing_deps[*]}"
        echo ""
        echo "Please install the missing dependencies:"
        echo "  Docker: https://docs.docker.com/get-docker/"
        echo "  Docker Compose: https://docs.docker.com/compose/install/"
        exit 1
    fi
    
    print_success "All prerequisites satisfied"
}

# Setup Ansible files
setup_ansible() {
    print_step "Setting up Ansible configuration"
    
    mkdir -p ansible
    
    # Check for playbook
    if [ -f "ansible/check-os-updates.yml" ]; then
        print_info "Found existing Ansible playbook"
    else
        print_info "Looking for your Ansible playbook..."
        
        # Common locations
        local common_locations=(
            "$HOME/check-os-updates.yml"
            "$HOME/Scripts/check-os-updates.yml"
            "$HOME/ansible/check-os-updates.yml"
        )
        
        local found_playbook=""
        for location in "${common_locations[@]}"; do
            if [ -f "$location" ]; then
                found_playbook="$location"
                break
            fi
        done
        
        if [ -n "$found_playbook" ]; then
            print_success "Found playbook at: $found_playbook"
            cp "$found_playbook" ansible/check-os-updates.yml
        else
            echo ""
            read -p "Enter path to your Ansible playbook: " playbook_path
            if [ -f "$playbook_path" ]; then
                cp "$playbook_path" ansible/check-os-updates.yml
                print_success "Copied Ansible playbook"
            else
                print_error "Playbook not found at: $playbook_path"
                exit 1
            fi
        fi
    fi
    
    # Check for inventory
    if [ -f "ansible/hosts" ]; then
        print_info "Found existing Ansible inventory"
    else
        print_info "Looking for your Ansible inventory..."
        
        local common_locations=(
            "$HOME/hosts"
            "$HOME/ansible/hosts"
            "$HOME/Scripts/hosts"
        )
        
        local found_inventory=""
        for location in "${common_locations[@]}"; do
            if [ -f "$location" ]; then
                found_inventory="$location"
                break
            fi
        done
        
        if [ -n "$found_inventory" ]; then
            print_success "Found inventory at: $found_inventory"
            cp "$found_inventory" ansible/hosts
        else
            echo ""
            read -p "Enter path to your Ansible inventory (hosts file): " inventory_path
            if [ -f "$inventory_path" ]; then
                cp "$inventory_path" ansible/hosts
                print_success "Copied Ansible inventory"
            else
                print_error "Inventory not found at: $inventory_path"
                exit 1
            fi
        fi
    fi
    
    print_success "Ansible configuration ready"
}

# Build and start services
start_services() {
    print_step "Starting PatchPilot"
    
    print_info "Building Docker images..."
    $DOCKER_COMPOSE_CMD build
    
    print_info "Starting services..."
    $DOCKER_COMPOSE_CMD up -d
    
    print_info "Waiting for services to be ready..."
    sleep 10
    
    # Health check
    if curl -s -f http://localhost:8000/ > /dev/null 2>&1; then
        print_success "Backend is healthy"
    else
        print_warning "Backend might still be starting up..."
    fi
    
    if curl -s -f http://localhost:8080/ > /dev/null 2>&1; then
        print_success "Frontend is healthy"
    else
        print_warning "Frontend might still be starting up..."
    fi
}

# Show completion message
show_completion() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${GREEN}🎉 PatchPilot Installation Complete!${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo -e "${BLUE}📊 Dashboard:${NC}  http://localhost:8080"
    echo -e "${BLUE}🔌 API:${NC}        http://localhost:8000"
    echo ""
    echo -e "${PURPLE}Useful Commands:${NC}"
    echo "  View logs:      $DOCKER_COMPOSE_CMD logs -f"
    echo "  Stop services:  $DOCKER_COMPOSE_CMD down"
    echo "  Restart:        $DOCKER_COMPOSE_CMD restart"
    echo "  Update:         git pull && $DOCKER_COMPOSE_CMD up -d --build"
    echo ""
    echo -e "${YELLOW}Next Steps:${NC}"
    echo "  1. Open the dashboard (opening in browser...)"
    echo "  2. Wait 30 seconds for initial system check"
    echo "  3. Start managing your patches!"
    echo ""
    
    # Try to open in browser
    if command -v open &> /dev/null; then
        sleep 2
        open http://localhost:8080
    elif command -v xdg-open &> /dev/null; then
        sleep 2
        xdg-open http://localhost:8080
    fi
}

# Main installation flow
main() {
    print_banner
    check_root
    check_prerequisites
    setup_ansible
    start_services
    show_completion
}

# Run installer
main
