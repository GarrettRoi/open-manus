#!/usr/bin/env python3
"""
Railway GraphQL API Client
Provides methods for interacting with Railway's GraphQL API
"""

import os
import json
import requests
from typing import Dict, Any, Optional, List


class RailwayClient:
    """Client for Railway GraphQL API"""
    
    def __init__(self, token: Optional[str] = None):
        """
        Initialize Railway API client
        
        Args:
            token: Railway API token (defaults to RAILWAY_TOKEN or RailwayAPI env var)
        """
        self.endpoint = "https://backboard.railway.com/graphql/v2"
        self.token = token or os.getenv("RAILWAY_TOKEN") or os.getenv("RailwayAPI")
        
        if not self.token:
            raise ValueError(
                "Railway token not found. Set RAILWAY_TOKEN or RailwayAPI environment variable."
            )
        
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def execute_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a GraphQL query"""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        
        response = requests.post(self.endpoint, headers=self.headers, json=payload)
        response.raise_for_status()
        
        result = response.json()
        if "errors" in result:
            raise Exception(f"GraphQL Error: {json.dumps(result['errors'], indent=2)}")
        
        return result.get("data", {})
    
    def create_project(self, name: str, description: str = "") -> Dict[str, Any]:
        """Create a new project"""
        query = """
        mutation projectCreate($input: ProjectCreateInput!) {
            projectCreate(input: $input) {
                id
                name
                description
            }
        }
        """
        variables = {"input": {"name": name, "description": description}}
        return self.execute_query(query, variables)["projectCreate"]
    
    def get_project_environments(self, project_id: str) -> List[Dict[str, Any]]:
        """Get environments for a project"""
        query = """
        query project($id: String!) {
            project(id: $id) {
                environments {
                    edges {
                        node {
                            id
                            name
                        }
                    }
                }
            }
        }
        """
        variables = {"id": project_id}
        result = self.execute_query(query, variables)
        edges = result.get("project", {}).get("environments", {}).get("edges", [])
        return [edge["node"] for edge in edges]
    
    def create_service_from_github(
        self, 
        project_id: str, 
        name: str, 
        repo: str,
        branch: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a service from GitHub repository"""
        repo_url = f"https://github.com/{repo}"
        if branch:
            repo_url = f"{repo_url}/tree/{branch}"
        
        query = """
        mutation serviceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) {
                id
                name
            }
        }
        """
        variables = {
            "input": {
                "projectId": project_id,
                "name": name,
                "source": {"repo": repo_url}
            }
        }
        return self.execute_query(query, variables)["serviceCreate"]
    
    def create_service_from_docker(
        self, 
        project_id: str, 
        name: str, 
        image: str
    ) -> Dict[str, Any]:
        """Create a service from Docker image"""
        query = """
        mutation serviceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) {
                id
                name
            }
        }
        """
        variables = {
            "input": {
                "projectId": project_id,
                "name": name,
                "source": {"image": image}
            }
        }
        return self.execute_query(query, variables)["serviceCreate"]
    
    def set_variables(
        self, 
        project_id: str, 
        environment_id: str, 
        service_id: str, 
        variables: Dict[str, str]
    ) -> bool:
        """Set environment variables for a service"""
        query = """
        mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
            variableCollectionUpsert(input: $input)
        }
        """
        variables_input = {
            "input": {
                "projectId": project_id,
                "environmentId": environment_id,
                "serviceId": service_id,
                "variables": variables
            }
        }
        self.execute_query(query, variables_input)
        return True
    
    def configure_service(
        self, 
        service_id: str, 
        environment_id: str, 
        settings: Dict[str, Any]
    ) -> bool:
        """Configure service settings"""
        query = """
        mutation serviceInstanceUpdate(
            $serviceId: String!, 
            $environmentId: String!, 
            $input: ServiceInstanceUpdateInput!
        ) {
            serviceInstanceUpdate(
                serviceId: $serviceId, 
                environmentId: $environmentId, 
                input: $input
            )
        }
        """
        variables = {
            "serviceId": service_id,
            "environmentId": environment_id,
            "input": settings
        }
        self.execute_query(query, variables)
        return True
    
    def create_volume(
        self, 
        project_id: str, 
        service_id: str, 
        mount_path: str,
        name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a volume for a service"""
        query = """
        mutation volumeCreate($input: VolumeCreateInput!) {
            volumeCreate(input: $input) {
                id
            }
        }
        """
        volume_input = {
            "projectId": project_id,
            "serviceId": service_id,
            "mountPath": mount_path
        }
        if name:
            volume_input["name"] = name
        
        variables = {"input": volume_input}
        return self.execute_query(query, variables)["volumeCreate"]
    
    def add_railway_domain(
        self, 
        service_id: str, 
        environment_id: str
    ) -> Dict[str, Any]:
        """Add a Railway-provided domain to a service"""
        query = """
        mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
            serviceDomainCreate(input: $input) {
                domain
            }
        }
        """
        variables = {
            "input": {
                "serviceId": service_id,
                "environmentId": environment_id
            }
        }
        return self.execute_query(query, variables)["serviceDomainCreate"]
    
    def deploy_service(
        self, 
        service_id: str, 
        environment_id: str
    ) -> bool:
        """Trigger a deployment for a service"""
        query = """
        mutation serviceInstanceDeploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceDeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        variables = {
            "serviceId": service_id,
            "environmentId": environment_id
        }
        self.execute_query(query, variables)
        return True
    
    def get_deployments(
        self, 
        project_id: str, 
        service_id: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Get recent deployments for a service"""
        query = """
        query deployments($input: DeploymentListInput!, $limit: Int!) {
            deployments(input: $input, first: $limit) {
                edges {
                    node {
                        id
                        status
                        createdAt
                        staticUrl
                    }
                }
            }
        }
        """
        variables = {
            "input": {
                "projectId": project_id,
                "serviceId": service_id
            },
            "limit": limit
        }
        result = self.execute_query(query, variables)
        edges = result.get("deployments", {}).get("edges", [])
        return [edge["node"] for edge in edges]
    
    def get_deployment_logs(
        self, 
        deployment_id: str, 
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get logs for a deployment"""
        query = """
        query deploymentLogs($deploymentId: String!, $limit: Int) {
            deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
                timestamp
                message
                severity
            }
        }
        """
        variables = {
            "deploymentId": deployment_id,
            "limit": limit
        }
        return self.execute_query(query, variables).get("deploymentLogs", [])
    
    def get_service_variables(
        self, 
        project_id: str, 
        environment_id: str, 
        service_id: str
    ) -> Dict[str, str]:
        """Get environment variables for a service"""
        query = """
        query variables($projectId: String!, $environmentId: String!, $serviceId: String) {
            variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
        """
        variables = {
            "projectId": project_id,
            "environmentId": environment_id,
            "serviceId": service_id
        }
        return self.execute_query(query, variables).get("variables", {})
