#!/usr/bin/env python3
"""
Template Functions
Implements Railway template variable functions like ${{secret()}} and ${{randomInt()}}
"""

import re
import random
import string
from typing import Dict, Any, Optional


def generate_secret(length: int = 32, alphabet: Optional[str] = None) -> str:
    """
    Generate a random secret string
    
    Args:
        length: Length of the secret (default: 32)
        alphabet: Character set to use (default: alphanumeric)
        
    Returns:
        Random secret string
    """
    if alphabet is None:
        alphabet = string.ascii_letters + string.digits
    
    return ''.join(random.choice(alphabet) for _ in range(length))


def generate_random_int(min_val: int = 0, max_val: int = 100) -> int:
    """
    Generate a random integer between min and max
    
    Args:
        min_val: Minimum value (default: 0)
        max_val: Maximum value (default: 100)
        
    Returns:
        Random integer
    """
    return random.randint(min_val, max_val)


def resolve_service_reference(
    services: Dict[str, Dict[str, Any]], 
    reference: str
) -> str:
    """
    Resolve a service reference like ${{SERVICE.postgres.DATABASE_URL}}
    
    Args:
        services: Dictionary of service names to their variables
        reference: Reference string (e.g., "SERVICE.postgres.DATABASE_URL")
        
    Returns:
        Resolved variable value
    """
    # Parse reference: SERVICE.service_name.variable_name
    parts = reference.split('.')
    if len(parts) != 3 or parts[0] != 'SERVICE':
        raise ValueError(f"Invalid service reference: {reference}")
    
    service_name = parts[1]
    variable_name = parts[2]
    
    if service_name not in services:
        raise ValueError(f"Service not found: {service_name}")
    
    if variable_name not in services[service_name]:
        raise ValueError(f"Variable not found: {variable_name} in service {service_name}")
    
    return services[service_name][variable_name]


def parse_template_variable(value: str, context: Dict[str, Any]) -> str:
    """
    Parse and resolve a template variable string
    
    Supports:
    - ${{secret(length, alphabet)}}
    - ${{randomInt(min, max)}}
    - ${{SERVICE.service_name.variable_name}}
    
    Args:
        value: Variable value string (may contain template functions)
        context: Context dictionary with 'services' key
        
    Returns:
        Resolved value string
    """
    if not isinstance(value, str):
        return value
    
    # Pattern to match ${{...}}
    pattern = r'\$\{\{([^}]+)\}\}'
    
    def replace_function(match):
        func_call = match.group(1).strip()
        
        # Handle secret() function
        if func_call.startswith('secret'):
            # Parse arguments: secret(32, "abc123")
            args_match = re.match(r'secret\(([^)]*)\)', func_call)
            if args_match:
                args_str = args_match.group(1)
                if args_str:
                    args = [arg.strip().strip('"\'') for arg in args_str.split(',')]
                    length = int(args[0]) if len(args) > 0 and args[0] else 32
                    alphabet = args[1] if len(args) > 1 and args[1] else None
                    return generate_secret(length, alphabet)
                else:
                    return generate_secret()
            return generate_secret()
        
        # Handle randomInt() function
        elif func_call.startswith('randomInt'):
            # Parse arguments: randomInt(0, 100)
            args_match = re.match(r'randomInt\(([^)]*)\)', func_call)
            if args_match:
                args_str = args_match.group(1)
                if args_str:
                    args = [arg.strip() for arg in args_str.split(',')]
                    min_val = int(args[0]) if len(args) > 0 and args[0] else 0
                    max_val = int(args[1]) if len(args) > 1 and args[1] else 100
                    return str(generate_random_int(min_val, max_val))
                else:
                    return str(generate_random_int())
            return str(generate_random_int())
        
        # Handle SERVICE references
        elif func_call.startswith('SERVICE.'):
            services = context.get('services', {})
            return resolve_service_reference(services, func_call)
        
        # Unknown function, return as-is
        return match.group(0)
    
    # Replace all template functions in the value
    return re.sub(pattern, replace_function, value)


def resolve_all_variables(
    variables: Dict[str, str], 
    context: Dict[str, Any]
) -> Dict[str, str]:
    """
    Resolve all template variables in a dictionary
    
    Args:
        variables: Dictionary of variable names to values (may contain template functions)
        context: Context dictionary with 'services' key
        
    Returns:
        Dictionary with all template functions resolved
    """
    resolved = {}
    for key, value in variables.items():
        resolved[key] = parse_template_variable(value, context)
    return resolved


def generate_database_url(
    service_name: str,
    db_type: str,
    username: str = "postgres",
    password: Optional[str] = None,
    database: Optional[str] = None,
    port: Optional[int] = None
) -> str:
    """
    Generate a database connection URL
    
    Args:
        service_name: Name of the database service
        db_type: Type of database (postgres, mysql, mongodb, redis)
        username: Database username
        password: Database password (generated if None)
        database: Database name (defaults to service_name)
        port: Database port (uses default for db_type if None)
        
    Returns:
        Database connection URL
    """
    if password is None:
        password = generate_secret(32)
    
    if database is None:
        database = service_name
    
    # Default ports
    default_ports = {
        'postgres': 5432,
        'postgresql': 5432,
        'mysql': 3306,
        'mongodb': 27017,
        'redis': 6379
    }
    
    if port is None:
        port = default_ports.get(db_type.lower(), 5432)
    
    # URL formats
    if db_type.lower() in ['postgres', 'postgresql']:
        return f"postgresql://{username}:{password}@{service_name}.railway.internal:{port}/{database}"
    elif db_type.lower() == 'mysql':
        return f"mysql://{username}:{password}@{service_name}.railway.internal:{port}/{database}"
    elif db_type.lower() == 'mongodb':
        return f"mongodb://{username}:{password}@{service_name}.railway.internal:{port}/{database}"
    elif db_type.lower() == 'redis':
        return f"redis://:{password}@{service_name}.railway.internal:{port}"
    else:
        return f"{db_type}://{username}:{password}@{service_name}.railway.internal:{port}/{database}"
