#!/usr/bin/env python3
"""
PatchPilot - Admin Setup Script
Run this to create or reset the admin user.

Usage:
    docker exec -it patchpilot-backend python setup_admin.py
    
    # Or with arguments:
    docker exec -it patchpilot-backend python setup_admin.py --username myadmin --password mysecurepass
"""

import asyncio
import argparse
import getpass
import os
import sys

import asyncpg
import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


async def setup_admin(username: str, password: str, email: str = None):
    """Create or reset the admin user"""
    
    db_url = (
        f"postgresql://{os.getenv('POSTGRES_USER', 'patchpilot')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'patchpilot')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
        f"{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'patchpilot')}"
    )
    
    if not email:
        email = f"{username}@patchpilot.local"
    
    password_hash = hash_password(password)
    
    conn = await asyncpg.connect(db_url)
    
    try:
        # Check if users table exists
        table_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'users'
            )
        """)
        
        if not table_exists:
            print("❌ Users table doesn't exist. Run the auth migration first:")
            print("   docker exec -it patchpilot-db psql -U patchpilot -d patchpilot -f /docker-entrypoint-initdb.d/002_add_authentication.sql")
            return False
        
        # Upsert the admin user
        await conn.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active)
            VALUES ($1, $2, $3, 'admin', true)
            ON CONFLICT (username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                email = EXCLUDED.email,
                role = 'admin',
                is_active = true,
                updated_at = NOW()
        """, username, email, password_hash)
        
        # Clear any existing sessions for this user (force re-login)
        await conn.execute("""
            DELETE FROM sessions WHERE user_id = (
                SELECT id FROM users WHERE username = $1
            )
        """, username)
        
        print(f"✅ Admin user '{username}' created/updated successfully!")
        print(f"   Email: {email}")
        print(f"   Role: admin")
        print(f"   All existing sessions cleared.")
        print(f"\n   Login at: http://localhost:8080/login.html")
        return True
        
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="PatchPilot Admin Setup")
    parser.add_argument("--username", "-u", help="Admin username")
    parser.add_argument("--password", "-p", help="Admin password (prompted if not provided)")
    parser.add_argument("--email", "-e", help="Admin email (optional)")
    args = parser.parse_args()
    
    print("=" * 50)
    print("  PatchPilot - Admin User Setup")
    print("=" * 50)
    print()
    
    username = args.username
    if not username:
        username = input("Enter admin username: ").strip()
        if not username:
            print("❌ Username cannot be empty")
            sys.exit(1)
    
    password = args.password
    if not password:
        password = getpass.getpass("Enter admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("❌ Passwords don't match")
            sys.exit(1)
    
    if len(password) < 8:
        print("❌ Password must be at least 8 characters")
        sys.exit(1)
    
    success = asyncio.run(setup_admin(username, password, args.email))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
