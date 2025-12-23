"""
API Client for E2E tests.

Provides high-level API client matching how the frontend calls backend services.
"""

import logging
import time
import os
from typing import Dict, Any, Optional, List
import requests
from config.env import config

logger = logging.getLogger(__name__)


class DagKnowsAPIClient:
    """
    High-level API client for DagKnows E2E tests.
    
    This client matches how the frontend (dagknows_nuxt) calls backend APIs.
    """
    
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        """
        Initialize API client.
        
        Args:
            base_url: Base URL (defaults to config)
            token: JWT token (defaults to config)
        """
        self.base_url = base_url or config.BASE_URL
        self.token = token or config.JWT_TOKEN
        self.proxy_param = config.PROXY_PARAM
        self.session = requests.Session()
        self.session.headers.update(config.get_auth_headers())
        
    def _get_url(self, endpoint: str, with_proxy: bool = True) -> str:
        """
        Get full URL with optional proxy parameter.
        
        Args:
            endpoint: API endpoint (e.g., "/api/v1/tasks/")
            with_proxy: Whether to add proxy parameter
            
        Returns:
            Full URL (proxy param will be added via params in _request, not here)
        """
        base = self.base_url.rstrip('/')
        endpoint = endpoint.lstrip('/')
        return f"{base}/{endpoint}"
    
    def _request(
        self,
        method: str,
        endpoint: str,
        with_proxy: bool = True,
        params: Optional[Dict[str, Any]] = None,
        allow_redirects: bool = True,
        **kwargs
    ) -> requests.Response:
        """
        Make HTTP request.
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            endpoint: API endpoint (e.g., "/api/v1/tasks/")
            with_proxy: Whether to add proxy parameter to query params
            params: Query parameters (merged with proxy param if needed)
            **kwargs: Additional request kwargs (json, headers, etc.)
        """
        url = self._get_url(endpoint, with_proxy=with_proxy)
        
        # Merge params with proxy param if needed
        request_params = params.copy() if params else {}
        
        # Add proxy parameter if needed
        if with_proxy and self.proxy_param:
            # proxy_param is like "?proxy=dev1" or "proxy=dev1"
            # Extract the key=value part
            proxy_str = self.proxy_param.lstrip('?')
            if '=' in proxy_str:
                proxy_key, proxy_value = proxy_str.split('=', 1)
                request_params[proxy_key] = proxy_value
        
        logger.debug(f"{method.upper()} {url}")
        if request_params:
            logger.debug(f"Query params: {request_params}")
        if 'json' in kwargs:
            logger.debug(f"Request payload: {kwargs['json']}")
        
        # Set allow_redirects in kwargs if not already set
        if 'allow_redirects' not in kwargs:
            kwargs['allow_redirects'] = allow_redirects
        
        response = self.session.request(method, url, params=request_params, **kwargs)
        
        # Log response status for debugging
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Response headers: {dict(response.headers)}")
        
        # Check if we got HTML instead of JSON (likely a redirect or error page)
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type and response.status_code == 200:
            logger.warning(f"Received HTML response instead of JSON. Status: {response.status_code}")
            logger.warning(f"Response preview: {response.text[:200]}")
            # Don't raise here, let the caller handle it
        
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            logger.error(f"HTTP Error: {e}")
            logger.error(f"Response: {response.text[:500]}")
            raise
        
        return response
    
    # ==================== TASK OPERATIONS ====================
    
    def create_task(self, task_data: Dict[str, Any], wsid: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a new task via API (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl('/api/tasks/'), {method: 'POST', body: {"task": task}})
        The /api/tasks/ endpoint is handled by req-router which forwards to /api/v1/tasks/ internally.
        We use /api/tasks/ to match the frontend exactly.
        
        Args:
            task_data: Task data dictionary
            wsid: Optional workspace ID (if not provided, will be set from task_data.workspace_ids or source_wsid)
            
        Returns:
            Created task response
        """
        # Match frontend behavior: wrap in "task" key
        payload = {"task": task_data}
        
        # Add workspace ID if provided
        params = {}
        if wsid:
            params["wsid"] = wsid
        
        # Use /api/tasks/ (same as frontend) - req-router will forward to /api/v1/tasks/ internally
        response = self._request("POST", "/api/tasks/", json=payload, params=params)
        return response.json()
    
    def get_task(self, task_id: str, wsid: Optional[str] = None) -> Dict[str, Any]:
        """
        Get task by ID via API (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/tasks/${taskId}/?wsid=${wsid}`))
        
        Args:
            task_id: Task ID
            wsid: Optional workspace ID
            
        Raises:
            requests.HTTPError: If task not found (404) or other HTTP error
        """
        params = {}
        if wsid:
            params["wsid"] = wsid
        
        # Use /api/tasks/ (same as frontend) - req-router will forward to /api/v1/tasks/ internally
        response = self._request("GET", f"/api/tasks/{task_id}", params=params)
        return response.json()
    
    def task_exists(self, task_id: str, wsid: Optional[str] = None) -> bool:
        """
        Check if a task exists without logging errors for expected 404s.
        
        Args:
            task_id: Task ID
            wsid: Optional workspace ID
            
        Returns:
            True if task exists, False if not found (404)
        """
        params = {}
        if wsid:
            params["wsid"] = wsid
        
        # Add proxy param if needed
        if self.proxy_param:
            proxy_str = self.proxy_param.lstrip('?')
            if '=' in proxy_str:
                proxy_key, proxy_value = proxy_str.split('=', 1)
                params[proxy_key] = proxy_value
        
        url = self._get_url(f"/api/tasks/{task_id}", with_proxy=False)
        
        try:
            # Make request directly to avoid error logging for expected 404s
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code == 404:
                return False
            response.raise_for_status()
            return True
        except requests.HTTPError:
            # For non-404 errors, re-raise
            raise
        except Exception:
            return False
    
    def update_task(
        self,
        task_id: str,
        task_data: Dict[str, Any],
        wsid: Optional[str] = None,
        update_mask: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Update task via API (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/tasks/${taskId}/?wsid=${wsid}`), {
            method: 'PATCH',
            body: {"task": patch, "update_mask": update_mask, "sub_task_ops": stops}
        })
        
        Args:
            task_id: Task ID
            task_data: Task object with fields to update
            wsid: Optional workspace ID
            update_mask: List of fields being updated (optional, frontend generates from task keys)
            
        Returns:
            Updated task response
        """
        params = {}
        if wsid:
            params["wsid"] = wsid
        
        # Frontend wraps in {"task": patch, "update_mask": update_mask}
        payload = {
            "task": task_data,
            "update_mask": update_mask or []
        }
        
        # Use /api/tasks/ (same as frontend) - req-router will forward to /api/v1/tasks/ internally
        # Frontend uses PATCH, not PUT
        response = self._request("PATCH", f"/api/tasks/{task_id}", json=payload, params=params)
        return response.json()
    
    def delete_task(self, task_id: str, wsid: Optional[str] = None, recurse: bool = True, forced: bool = False) -> Optional[Dict[str, Any]]:
        """
        Delete task via API (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/tasks/${task_id}/?recurse=${recurse}&wsid=${wsid}&forced=${forced}`), {method: 'DELETE'})
        
        Args:
            task_id: Task ID
            wsid: Optional workspace ID
            recurse: Whether to delete recursively
            forced: Whether to force delete
            
        Returns:
            Delete response or None
        """
        # Match frontend behavior:
        # - Always include a workspace ID (wsid)
        # - Default wsid to "__DEFAULT__" (or TEST_WORKSPACE env var) when not provided
        # - By default, delete recursively to ensure cleanup in tests
        params = {}

        # Determine workspace ID
        if wsid is None:
            # Prefer explicit test workspace if set, otherwise default
            wsid = os.getenv("TEST_WORKSPACE", "__DEFAULT__")

        if wsid:
            params["wsid"] = wsid
        if recurse:
            params["recurse"] = "true"
        if forced:
            params["forced"] = "true"
        
        try:
            # Use /api/tasks/ (same as frontend) - req-router will forward to /api/v1/tasks/ internally
            response = self._request("DELETE", f"/api/tasks/{task_id}", params=params)
            return response.json() if response.text else {}
        except requests.HTTPError as e:
            if e.response.status_code == 500:
                logger.warning(f"DELETE returned 500 (known backend issue), ignoring")
                return None
            raise
    
    def list_tasks(
        self,
        page_size: int = 20,
        page_key: Optional[str] = None,
        query: Optional[str] = None,
        wsid: Optional[str] = None,
        **filters
    ) -> Dict[str, Any]:
        """
        List tasks via API (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl('/api/tasks/?page_key=...&page_size=...'))
        
        Args:
            page_size: Number of tasks per page
            page_key: Pagination key
            query: Search query (uses KNN search)
            wsid: Optional workspace ID
            **filters: Additional filters (tags, etc.)
            
        Returns:
            List response with tasks
        """
        params = {"page_size": page_size}
        if page_key:
            params["page_key"] = page_key
        if query:
            params["q"] = query  # KNN search like UI does
        if wsid:
            params["wsid"] = wsid
        params.update(filters)
        
        # Use /api/tasks/ (same as frontend) - req-router will forward to /api/v1/tasks/ internally
        response = self._request("GET", "/api/tasks/", params=params)
        return response.json()
    
    # ==================== ALERT OPERATIONS ====================
    
    def process_alert(self, alert_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send alert to processAlert endpoint.
        
        Args:
            alert_payload: Grafana or PagerDuty alert payload
            
        Returns:
            Alert processing response
        """
        url = config.get_process_alert_url()
        logger.info(f"Processing alert via {url}")
        
        response = self.session.post(
            url,
            json=alert_payload,
            headers=config.get_auth_headers()
        )
        response.raise_for_status()
        return response.json()
    
    def search_alerts(self, **params) -> Dict[str, Any]:
        """Search alerts."""
        response = self._request("GET", "/api/alerts", params=params)
        return response.json()
    
    def get_alert(self, alert_id: str) -> Dict[str, Any]:
        """Get alert by ID."""
        response = self._request("GET", f"/api/alerts/{alert_id}")
        return response.json()
    
    # ==================== JOB OPERATIONS ====================
    
    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get job execution status.
        
        Args:
            job_id: Job ID
            
        Returns:
            Job status response
        """
        response = self._request("GET", f"/api/v1/jobs/{job_id}")
        return response.json()
    
    def wait_for_job_completion(
        self,
        job_id: str,
        timeout: int = 60,
        poll_interval: int = 2
    ) -> Dict[str, Any]:
        """
        Wait for job to complete.
        
        Args:
            job_id: Job ID
            timeout: Max wait time in seconds
            poll_interval: Polling interval in seconds
            
        Returns:
            Final job status
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            status = self.get_job_status(job_id)
            state = status.get("status", "").upper()
            
            if state in ["COMPLETED", "FAILED", "SUCCESS"]:
                logger.info(f"Job {job_id} finished with state: {state}")
                return status
            
            logger.debug(f"Job {job_id} status: {state}, waiting...")
            time.sleep(poll_interval)
        
        raise TimeoutError(f"Job {job_id} did not complete within {timeout} seconds")
    
    # ==================== WORKSPACE OPERATIONS ====================
    
    def list_workspaces(self, workspace_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        List workspaces (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl('/api/workspaces/'))
        
        Args:
            workspace_ids: Optional list of workspace IDs to fetch
            
        Returns:
            List of workspace objects
        """
        params = {}
        if workspace_ids:
            params["ids"] = ",".join(workspace_ids)
        
        response = self._request("GET", "/api/workspaces/", params=params)
        workspaces_dict = response.json().get("workspaces", {})
        
        # Convert dict to list (frontend does this)
        result = []
        if isinstance(workspaces_dict, dict):
            for workspace_id, workspace in workspaces_dict.items():
                if workspace:
                    result.append(workspace)
        elif isinstance(workspaces_dict, list):
            result = workspaces_dict
        
        return result
    
    def get_workspace_by_name(self, workspace_name: str) -> Optional[Dict[str, Any]]:
        """
        Get workspace by name.
        
        Args:
            workspace_name: Workspace name/title
            
        Returns:
            Workspace object or None if not found
        """
        workspaces = self.list_workspaces()
        for workspace in workspaces:
            if workspace.get("title", "").lower() == workspace_name.lower():
                return workspace
        return None
    
    # ==================== SETTINGS OPERATIONS ====================
    
    def get_admin_settings(self) -> Dict[str, Any]:
        """Get admin settings."""
        response = self._request("POST", "/getSettings", json={}, with_proxy=True)
        return response.json()
    
    def set_incident_response_mode(self, mode: str) -> Dict[str, Any]:
        """
        Set incident response mode.
        
        Args:
            mode: One of 'deterministic', 'ai_selected', 'autonomous'
            
        Returns:
            Response from setFlags API
        """
        payload = {"incident_response_mode": mode}
        response = self._request("POST", "/setFlags", json=payload, with_proxy=True)
        return response.json()
    
    # ==================== AI/CHAT OPERATIONS ====================
    
    def start_chat_session(self, prompt: str, context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Start AI chat session.
        
        Args:
            prompt: Initial prompt
            context: Optional context for chat
            
        Returns:
            Chat session response
        """
        payload = {
            "prompt": prompt,
            "context": context or {}
        }
        response = self._request("POST", "/api/chat/start", json=payload)
        return response.json()
    
    def send_chat_message(self, session_id: str, message: str) -> Dict[str, Any]:
        """
        Send message in existing chat session.
        
        Args:
            session_id: Chat session ID
            message: Message to send
            
        Returns:
            AI response
        """
        payload = {
            "session_id": session_id,
            "message": message
        }
        response = self._request("POST", "/api/chat/message", json=payload)
        return response.json()
    
    # ==================== USER OPERATIONS ====================
    
    def list_users(self, user_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        List organization users (matches backend API behavior).
        
        Uses GET /api/users/?ids=all to get user IDs, then fetches full user details.
        This is the proper API endpoint (BaseUserResource) that works with JWT authentication.
        
        Note: /get_org_users uses @login_required (session-based) and redirects to HTML,
        so we use /api/users/?ids=all instead which uses JWT token authentication.
        
        Backend: req_router/src/tasks.py BaseUserResource.get() -> getAllUsersForOrg()
        Frontend: composables/getUsersList.js uses this endpoint
        
        Args:
            user_ids: Optional list of user IDs to fetch (defaults to 'all')
            
        Returns:
            List of user objects with id, name, email, etc.
        """
        # Step 1: Get list of users with their IDs using /api/users/?ids=all
        params = {"ids": "all"}
        response = self._request("GET", "/api/users/", params=params, with_proxy=True)
        result = response.json()
        
        if not isinstance(result, dict) or "users" not in result:
            logger.warning(f"Unexpected response format from /api/users/?ids=all: {type(result)}")
            return []
        
        users_list = result["users"]  # List of {id, name}
        logger.debug(f"Retrieved {len(users_list)} users from /api/users/?ids=all")
        
        # Step 2: The backend getAllUsersForOrg() only returns id and name
        # We need to get email. Since getUser() accepts email, we can try to get user details
        # by fetching user info. However, the endpoint structure when passing IDs is different.
        # For now, let's enhance the user objects with email by fetching them individually
        # if needed, OR we can try a different approach.
        
        # Actually, let's check if we can get email by querying with user IDs
        # But the endpoint when passing IDs returns a dict, not a list
        # Let's build a list with the available info and add email if we can fetch it
        
        enhanced_users = []
        for user_info in users_list:
            user_id = user_info.get("id")
            # Try to get full user details by ID
            # The getUser method accepts either ID or email, so we can try fetching by ID
            try:
                # Fetch user details by ID - the endpoint returns {id: {...}} format
                user_detail_response = self._request("GET", "/api/users/", params={"ids": str(user_id)}, with_proxy=True)
                user_detail_result = user_detail_response.json()
                
                if isinstance(user_detail_result, dict) and str(user_id) in user_detail_result:
                    user_detail = user_detail_result[str(user_id)]
                    # Merge the detail info
                    enhanced_user = {**user_info, **user_detail}
                    # Try to get email - if not in detail, we might need to query by email
                    if "email" not in enhanced_user:
                        # Email might not be in the response, so we'll keep what we have
                        pass
                    enhanced_users.append(enhanced_user)
                else:
                    # Fallback: use what we have
                    enhanced_users.append(user_info)
            except Exception as e:
                logger.warning(f"Could not fetch details for user {user_id}: {e}")
                # Fallback: use what we have
                enhanced_users.append(user_info)
        
        return enhanced_users
    
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get user by email address.
        
        Uses /api/users/?ids={email} to fetch user by email directly.
        The backend getUser() method accepts either ID or email.
        
        Args:
            email: User email address
            
        Returns:
            User object or None if not found
        """
        # Try to fetch user directly by email (getUser accepts email)
        try:
            response = self._request("GET", "/api/users/", params={"ids": email}, with_proxy=True)
            result = response.json()
            
            # When passing email as ID, backend returns {email: {...}}
            if isinstance(result, dict) and email in result:
                user = result[email]
                # Add email to the user object if not present
                if "email" not in user:
                    user["email"] = email
                logger.debug(f"Found user by email: {email} (id: {user.get('id')})")
                return user
        except Exception as e:
            logger.debug(f"Could not fetch user directly by email {email}: {e}")
        
        # Fallback: search in list of users
        users = self.list_users()
        logger.debug(f"Searching for user with email '{email}' in {len(users)} users")
        
        for user in users:
            # Check both 'email' and 'userid' fields (frontend uses both)
            user_email = user.get("email", "") or user.get("userid", "")
            if user_email and user_email.lower() == email.lower():
                logger.debug(f"Found user: {user_email} (id: {user.get('id')})")
                return user
        
        # Log available emails for debugging
        available_emails = [u.get("email") or u.get("userid", "") for u in users[:5] if u.get("email") or u.get("userid")]
        logger.debug(f"Available user emails (first 5): {available_emails}")
        return None
    
    # ==================== IAM/ROLE OPERATIONS ====================
    
    def list_roles(self) -> List[Dict[str, Any]]:
        """
        List all roles (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl('/api/iam/roles'), { method: "GET" })
        
        Returns:
            List of role objects
        """
        response = self._request("GET", "/api/iam/roles")
        return response.json().get("roles", [])
    
    def get_role_by_name(self, role_name: str, path: str = "dkroles") -> Optional[Dict[str, Any]]:
        """
        Get role by name (matches frontend behavior).
        
        Frontend calls: GET /api/iam/roles and filters by name and path
        
        Args:
            role_name: Role name
            path: Role path (default: "dkroles" for application roles)
            
        Returns:
            Role object or None if not found
        """
        roles = self.list_roles()
        for role in roles:
            if (role.get("name", "").lower() == role_name.lower() and 
                role.get("path", "") == path):
                return role
        return None
    
    def create_role(self, role_name: str, path: str = "dkroles") -> Dict[str, Any]:
        """
        Create a new role (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl("/api/iam/roles"), {
            method: "POST",
            body: {"role": {"path": "dkroles", "name": role_name}}
        })
        
        Args:
            role_name: Role name
            path: Role path (default: "dkroles" for application roles)
            
        Returns:
            Created role object
        """
        payload = {
            "role": {
                "path": path,
                "name": role_name
            }
        }
        response = self._request("POST", "/api/iam/roles", json=payload)
        return response.json().get("role", {})
    
    def assign_role_to_user(
        self,
        user_id: str,
        role_name: str,
        resource_type: str = "workspace",
        resource_id: str = "",
        path: str = "dkroles"
    ) -> Dict[str, Any]:
        """
        Assign a role to a user for a resource (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/iam/users/${userid}/roles`), {
            method: "POST",
            body: {"added_roles": [...], "removed_roles": [...]}
        })
        
        Args:
            user_id: User ID
            role_name: Role name to assign
            resource_type: Resource type (default: "workspace")
            resource_id: Resource ID (workspace ID, empty string for default workspace)
            path: Role path (default: "dkroles")
            
        Returns:
            Response from API
        """
        added_roles = [{
            "resource_type": resource_type,
            "resource_id": resource_id,
            "name": role_name,
            "path": path
        }]
        
        payload = {
            "added_roles": added_roles,
            "removed_roles": []
        }
        
        response = self._request("POST", f"/api/iam/users/{user_id}/roles", json=payload)
        return response.json()
    
    def remove_role_from_user(
        self,
        user_id: str,
        role_name: str,
        resource_type: str = "workspace",
        resource_id: str = "",
        path: str = "dkroles"
    ) -> Dict[str, Any]:
        """
        Remove a role from a user for a resource (matches frontend behavior).
        
        Args:
            user_id: User ID
            role_name: Role name to remove
            resource_type: Resource type (default: "workspace")
            resource_id: Resource ID (workspace ID, empty string for default workspace)
            path: Role path (default: "dkroles")
            
        Returns:
            Response from API
        """
        removed_roles = [{
            "resource_type": resource_type,
            "resource_id": resource_id,
            "name": role_name,
            "path": path
        }]
        
        payload = {
            "added_roles": [],
            "removed_roles": removed_roles
        }
        
        response = self._request("POST", f"/api/iam/users/{user_id}/roles", json=payload)
        return response.json()
    
    def get_user_roles(self, user_id: str, map_ids_to_names: bool = True) -> Dict[str, List[str]]:
        """
        Get roles assigned to a user (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/iam/users/${userid}/roles`), { method: "GET" })
        
        Args:
            user_id: User ID
            map_ids_to_names: If True, map workspace IDs to workspace names (default: True)
            
        Returns:
            Dictionary mapping workspace names (or IDs if map_ids_to_names=False) to lists of role names
            Example: {"Default": ["Admin"], "DEV": ["read1"]} or {"": ["Admin"], "workspace-id-123": ["read1"]}
            
        Note: The API returns resource_id which is the workspace ID. By default, we map IDs to names
        for better usability, but the frontend uses IDs directly as keys.
        """
        response = self._request("GET", f"/api/iam/users/{user_id}/roles")
        rassocs = response.json().get("rassocs", [])
        
        out = {}
        workspace_id_to_name = {}
        
        # Build workspace ID to name mapping if needed
        if map_ids_to_names:
            workspaces = self.list_workspaces()
            for workspace in workspaces:
                workspace_id_to_name[workspace.get("id", "")] = workspace.get("title", "")
        
        for rassoc in rassocs:
            restype = rassoc.get("resource_type", "")
            if restype != "proxy":
                resource_id = rassoc.get("resource_id", "")
                # Map empty resource_id to "Default" (frontend does this)
                if not resource_id or resource_id == "undefined":
                    resource_id = "Default"
                    key = "Default"
                elif map_ids_to_names and resource_id in workspace_id_to_name:
                    # Map workspace ID to workspace name
                    key = workspace_id_to_name[resource_id]
                else:
                    # Use resource_id as-is (could be ID or name)
                    key = resource_id
                
                if key not in out:
                    out[key] = []
                out[key].append(rassoc.get("role_name", ""))
        
        return out
    
    def get_privileges(self) -> List[str]:
        """
        Get all available privileges (matches frontend behavior).
        
        Frontend calls: logFetchAJAX(getUrl(`/api/iam/roles/privileges`), { method: "GET" })
        
        Returns:
            List of privilege names (strings)
        """
        response = self._request("GET", "/api/iam/roles/privileges")
        privileges = response.json().get("privileges", [])
        # Return only privilege names (strings) like frontend does
        return [p.get("name") for p in privileges if p.get("name")]
    
    def get_all_roles_and_privileges(self) -> Dict[str, List[str]]:
        """
        Get all roles and their assigned privileges (matches frontend behavior).
        
        Frontend calls: GET /api/iam/roles and filters for dkroles
        
        Returns:
            Dictionary mapping role names to lists of privilege names
        """
        result = {}
        
        # Admin has all privileges
        all_privileges = self.get_privileges()
        result["Admin"] = all_privileges
        
        # Get all roles
        roles = self.list_roles()
        for role in roles:
            if role.get("path") == "dkroles":
                role_name = role.get("name", "")
                if role_name.lower() != "admin":
                    result[role_name] = role.get("permissions", [])
        
        return result
    
    def assign_privileges_to_role(
        self,
        role_id: str,
        privileges: List[str]
    ) -> Dict[str, Any]:
        """
        Assign privileges to a role (matches frontend behavior).
        
        Frontend calls: PUT /api/iam/roles/{roleid} with {
            "added_permissions": [...],
            "role": role_object,
            "update_mask": [...]
        }
        
        Args:
            role_id: Role ID (role name for dkroles)
            privileges: List of privilege names to add
            
        Returns:
            Updated role object
        """
        # First get the role to get its current state
        role = self.get_role_by_name(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")
        
        if "permissions" not in role:
            role["permissions"] = []
        
        payload = {
            "added_permissions": privileges,
            "role": role,
            "update_mask": list(role.keys())
        }
        
        response = self._request("PUT", f"/api/iam/roles/{role_id}", json=payload)
        return response.json().get("role", {})
    
    def remove_privileges_from_role(
        self,
        role_id: str,
        privileges: List[str]
    ) -> Dict[str, Any]:
        """
        Remove privileges from a role (matches frontend behavior).
        
        Frontend calls: PUT /api/iam/roles/{roleid} with {
            "removed_permissions": [...],
            "role": role_object,
            "update_mask": [...]
        }
        
        Args:
            role_id: Role ID (role name for dkroles)
            privileges: List of privilege names to remove
            
        Returns:
            Updated role object
        """
        # First get the role to get its current state
        role = self.get_role_by_name(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")
        
        if "permissions" not in role:
            role["permissions"] = []
        
        payload = {
            "removed_permissions": privileges,
            "role": role,
            "update_mask": list(role.keys())
        }
        
        response = self._request("PUT", f"/api/iam/roles/{role_id}", json=payload)
        return response.json().get("role", {})
    
    def delete_role(self, role_name: str, path: str = "dkroles") -> Optional[Dict[str, Any]]:
        """
        Delete a role (matches frontend behavior).
        
        Frontend calls: DELETE /api/iam/roles/{roleid}
        
        For dkroles, the frontend uses just the role name (not "path:name" format).
        The backend handles both formats:
        - If no ":" in roleid, deletes all roles with that name (across all paths)
        - If ":" present, uses "path:name" format
        
        Since dkroles are unique by name within the dkroles path, using just the role name
        should work. The backend will find and delete the role by name.
        
        Args:
            role_name: Role name
            path: Role path (default: "dkroles" for application roles) - used for logging only
            
        Returns:
            Delete response or None
        """
        # Use just the role name (matches frontend behavior - deleteProxyRole uses just role name)
        # Backend will find the role by name and delete it
        # For dkroles, names are unique within the path, so this should work
        role_id = role_name
        
        try:
            logger.debug(f"Deleting role '{role_name}' (path: {path}) using role_id: {role_id}")
            response = self._request("DELETE", f"/api/iam/roles/{role_id}")
            return response.json() if response.text else {}
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"Role '{role_name}' not found (may already be deleted)")
                return None
            elif e.response.status_code == 500:
                # 500 error might indicate the role is in use or other backend issue
                logger.error(f"Role deletion returned 500 - role '{role_name}' may be in use or backend error occurred")
                logger.error(f"Response: {e.response.text[:500] if e.response.text else 'No response body'}")
                # Don't raise - log the error but allow test to complete
                return None
            raise


# Factory function
def create_api_client(
    base_url: Optional[str] = None,
    token: Optional[str] = None
) -> DagKnowsAPIClient:
    """
    Create API client instance.
    
    Args:
        base_url: Base URL (defaults to config)
        token: JWT token (defaults to config)
        
    Returns:
        DagKnowsAPIClient instance
    """
    return DagKnowsAPIClient(base_url=base_url, token=token)

