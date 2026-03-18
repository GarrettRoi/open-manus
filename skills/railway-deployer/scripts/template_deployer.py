#!/usr/bin/env python3
"""
Template Deployer
Deploys Railway templates using the Railway API
"""

import json
import time
from typing import Dict, Any, List, Optional
from pathlib import Path

from railway_client import RailwayClient
from template_functions import (
    resolve_all_variables,
    generate_database_url,
    generate_secret
)


class TemplateDeployer:
    """Deploy Railway templates via API"""
    
    def __init__(self, client: RailwayClient, template: Dict[str, Any]):
        """
        Initialize template deployer
        
        Args:
            client: RailwayClient instance
            template: Template definition dictionary
        """
        self.client = client
        self.template = template
        self.project_id = None
        self.environment_id = None
        self.services = {}  # service_name -> {id, variables}
        self.deployment_info = {}
    
    def validate_template(self) -> bool:
        """Validate template structure"""
        required_fields = ['name', 'services']
        for field in required_fields:
            if field not in self.template:
                raise ValueError(f"Template missing required field: {field}")
        
        if not isinstance(self.template['services'], list):
            raise ValueError("Template 'services' must be a list")
        
        for service in self.template['services']:
            if 'name' not in service:
                raise ValueError("Service missing 'name' field")
            if 'source' not in service:
                raise ValueError(f"Service '{service['name']}' missing 'source' field")
        
        return True
    
    def deploy(self) -> Dict[str, Any]:
        """
        Deploy the template
        
        Returns:
            Deployment information dictionary
        """
        print(f"\n🚀 Deploying template: {self.template['name']}")
        print(f"   Description: {self.template.get('description', 'N/A')}")
        
        # Validate template
        self.validate_template()
        print("✓ Template validated")
        
        # Create project
        self._create_project()
        print(f"✓ Project created: {self.project_id}")
        
        # Get production environment
        self._get_environment()
        print(f"✓ Environment: {self.environment_id}")
        
        # Deploy services
        self._deploy_services()
        print(f"✓ Services deployed: {len(self.services)}")
        
        # Generate deployment info
        self._generate_deployment_info()
        
        return self.deployment_info
    
    def _create_project(self):
        """Create the project"""
        result = self.client.create_project(
            name=self.template['name'],
            description=self.template.get('description', '')
        )
        self.project_id = result['id']
    
    def _get_environment(self):
        """Get the production environment ID"""
        environments = self.client.get_project_environments(self.project_id)
        
        # Find production environment
        for env in environments:
            if env['name'].lower() == 'production':
                self.environment_id = env['id']
                return
        
        # Fallback to first environment
        if environments:
            self.environment_id = environments[0]['id']
        else:
            raise Exception("No environments found in project")
    
    def _deploy_services(self):
        """Deploy all services in the template"""
        for service_config in self.template['services']:
            service_name = service_config['name']
            print(f"\n  Deploying service: {service_name}")
            
            # Create service
            service_id = self._create_service(service_config)
            print(f"    ✓ Service created: {service_id}")
            
            # Initialize service tracking
            self.services[service_name] = {
                'id': service_id,
                'variables': {},
                'config': service_config
            }
            
            # Set up database-specific variables
            if self._is_database_service(service_config):
                self._setup_database_variables(service_name, service_config)
            
            # Resolve and set variables
            if 'variables' in service_config:
                self._set_service_variables(service_name, service_config['variables'])
                print(f"    ✓ Variables set")
            
            # Configure service settings
            if 'settings' in service_config:
                self._configure_service_settings(service_name, service_config['settings'])
                print(f"    ✓ Settings configured")
            
            # Set up networking
            if service_config.get('networking', {}).get('public'):
                self._setup_networking(service_name)
                print(f"    ✓ Public networking enabled")
            
            # Create volumes
            if 'volumes' in service_config:
                self._create_volumes(service_name, service_config['volumes'])
                print(f"    ✓ Volumes created")
            
            # Small delay between services
            time.sleep(1)
    
    def _create_service(self, service_config: Dict[str, Any]) -> str:
        """Create a service"""
        source = service_config['source']
        name = service_config['name']
        
        if source['type'] == 'github':
            result = self.client.create_service_from_github(
                project_id=self.project_id,
                name=name,
                repo=source['repo'],
                branch=source.get('branch')
            )
        elif source['type'] == 'docker':
            result = self.client.create_service_from_docker(
                project_id=self.project_id,
                name=name,
                image=source['image']
            )
        else:
            raise ValueError(f"Unknown source type: {source['type']}")
        
        return result['id']
    
    def _is_database_service(self, service_config: Dict[str, Any]) -> bool:
        """Check if service is a database"""
        source = service_config.get('source', {})
        if source.get('type') == 'docker':
            image = source.get('image', '').lower()
            return any(db in image for db in ['postgres', 'mysql', 'mongodb', 'redis'])
        return False
    
    def _setup_database_variables(self, service_name: str, service_config: Dict[str, Any]):
        """Set up database-specific variables"""
        source = service_config.get('source', {})
        image = source.get('image', '').lower()
        
        # Generate password
        password = generate_secret(32)
        
        # Set up variables based on database type
        if 'postgres' in image:
            db_vars = {
                'POSTGRES_PASSWORD': password,
                'POSTGRES_USER': 'postgres',
                'POSTGRES_DB': service_name,
                'DATABASE_URL': generate_database_url(
                    service_name, 'postgres', 
                    password=password, 
                    database=service_name
                )
            }
        elif 'mysql' in image:
            db_vars = {
                'MYSQL_ROOT_PASSWORD': password,
                'MYSQL_DATABASE': service_name,
                'DATABASE_URL': generate_database_url(
                    service_name, 'mysql', 
                    username='root',
                    password=password, 
                    database=service_name
                )
            }
        elif 'mongodb' in image:
            db_vars = {
                'MONGO_INITDB_ROOT_USERNAME': 'admin',
                'MONGO_INITDB_ROOT_PASSWORD': password,
                'MONGO_INITDB_DATABASE': service_name,
                'DATABASE_URL': generate_database_url(
                    service_name, 'mongodb', 
                    username='admin',
                    password=password, 
                    database=service_name
                )
            }
        elif 'redis' in image:
            db_vars = {
                'REDIS_PASSWORD': password,
                'DATABASE_URL': generate_database_url(
                    service_name, 'redis', 
                    password=password
                )
            }
        else:
            return
        
        # Store in service variables for reference
        self.services[service_name]['variables'].update(db_vars)
    
    def _set_service_variables(self, service_name: str, variables: Dict[str, str]):
        """Set variables for a service"""
        service_id = self.services[service_name]['id']
        
        # Build context for variable resolution
        context = {'services': {}}
        for svc_name, svc_data in self.services.items():
            context['services'][svc_name] = svc_data['variables']
        
        # Resolve template variables
        resolved_vars = resolve_all_variables(variables, context)
        
        # Merge with existing variables (database vars)
        all_vars = {**self.services[service_name]['variables'], **resolved_vars}
        
        # Set variables via API
        self.client.set_variables(
            project_id=self.project_id,
            environment_id=self.environment_id,
            service_id=service_id,
            variables=all_vars
        )
        
        # Update stored variables
        self.services[service_name]['variables'].update(resolved_vars)
    
    def _configure_service_settings(self, service_name: str, settings: Dict[str, Any]):
        """Configure service settings"""
        service_id = self.services[service_name]['id']
        
        # Build settings input
        settings_input = {}
        if 'startCommand' in settings:
            settings_input['startCommand'] = settings['startCommand']
        if 'healthcheckPath' in settings:
            settings_input['healthcheckPath'] = settings['healthcheckPath']
        if 'rootDirectory' in settings:
            settings_input['rootDirectory'] = settings['rootDirectory']
        
        if settings_input:
            self.client.configure_service(
                service_id=service_id,
                environment_id=self.environment_id,
                settings=settings_input
            )
    
    def _setup_networking(self, service_name: str):
        """Set up public networking for a service"""
        service_id = self.services[service_name]['id']
        
        try:
            result = self.client.add_railway_domain(
                service_id=service_id,
                environment_id=self.environment_id
            )
            domain = result.get('domain')
            if domain:
                self.services[service_name]['domain'] = domain
        except Exception as e:
            print(f"    ⚠ Could not add domain: {e}")
    
    def _create_volumes(self, service_name: str, volumes: List[Dict[str, str]]):
        """Create volumes for a service"""
        service_id = self.services[service_name]['id']
        
        for volume_config in volumes:
            mount_path = volume_config['mountPath']
            self.client.create_volume(
                project_id=self.project_id,
                service_id=service_id,
                mount_path=mount_path,
                name=volume_config.get('name')
            )
    
    def _generate_deployment_info(self):
        """Generate deployment information"""
        self.deployment_info = {
            'project_id': self.project_id,
            'environment_id': self.environment_id,
            'template_name': self.template['name'],
            'services': {}
        }
        
        for service_name, service_data in self.services.items():
            service_info = {
                'id': service_data['id'],
                'name': service_name
            }
            
            # Add domain if available
            if 'domain' in service_data:
                service_info['url'] = f"https://{service_data['domain']}"
            
            # Add important variables (non-sensitive)
            if 'DATABASE_URL' in service_data['variables']:
                service_info['has_database_url'] = True
            
            self.deployment_info['services'][service_name] = service_info
    
    def get_deployment_info(self) -> Dict[str, Any]:
        """Get deployment information"""
        return self.deployment_info


def load_template(template_path: str) -> Dict[str, Any]:
    """Load a template from a JSON file"""
    with open(template_path, 'r') as f:
        return json.load(f)


def deploy_template(template_path: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Deploy a template from a file
    
    Args:
        template_path: Path to template JSON file
        token: Railway API token (optional, uses env var if not provided)
        
    Returns:
        Deployment information
    """
    # Load template
    template = load_template(template_path)
    
    # Create client
    client = RailwayClient(token)
    
    # Deploy
    deployer = TemplateDeployer(client, template)
    return deployer.deploy()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python template_deployer.py <template_path>")
        sys.exit(1)
    
    template_path = sys.argv[1]
    result = deploy_template(template_path)
    
    print("\n" + "="*60)
    print("Deployment Complete!")
    print("="*60)
    print(json.dumps(result, indent=2))
