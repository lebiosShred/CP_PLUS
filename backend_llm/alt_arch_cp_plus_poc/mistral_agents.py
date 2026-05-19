"""
P4 — Mistral Agents API Scaffolding
====================================
Utility module for creating Mistral-hosted agents with built-in connectors
(web search, code execution, document library).

ARCHITECTURE NOTE (Director Sundaram):
    This module creates agents on MISTRAL'S platform, which is SEPARATE from
    the watsonx Orchestrate agent layer. These are two distinct orchestration
    systems:
    
    - watsonx Orchestrate: Manages the CP Plus agent YAML, tool binding via
      OpenAPI, and the embed chat frontend. This is the PRIMARY orchestration.
    
    - Mistral Agents API: Provides built-in connectors (web search, code exec)
      that watsonx Orchestrate doesn't offer natively. These can be used as
      SUPPLEMENTARY capabilities called from the backend.
    
    Integration pattern: The unified_backend.py can call Mistral agents
    internally for specific tasks (e.g., web search for competitor pricing)
    while watsonx Orchestrate remains the user-facing orchestration layer.

Usage:
    from mistral_agents import create_web_search_agent, create_code_exec_agent
    
    # Create a web search agent for live product lookups
    agent = create_web_search_agent(client)
    
    # Query the agent
    response = client.agents.complete(
        agent_id=agent.id,
        messages=[{"role": "user", "content": "Latest CP Plus 8MP dome camera specs"}]
    )
"""

import logging
import os
from mistralai import Mistral

log = logging.getLogger("mistral-agents")


def create_web_search_agent(client: Mistral) -> object:
    """Creates a Mistral agent with web search capability.
    
    Use case: Live product lookups, competitor pricing, market research
    during bid preparation.
    
    Args:
        client: Initialized Mistral client with scale-as-you-go API key.
    
    Returns:
        Agent object with .id attribute for subsequent queries.
    """
    try:
        agent = client.agents.create(
            model="mistral-large-latest",
            name="CP Plus Market Research Agent",
            description="Searches the web for current CCTV product specs, pricing, and market intelligence.",
            instructions=(
                "You are a market research assistant for CP Plus, a leading CCTV manufacturer. "
                "Use web search to find:\n"
                "1. Current competitor product specifications (Hikvision, Dahua, Axis)\n"
                "2. Market pricing for surveillance equipment\n"
                "3. Latest industry standards and certifications\n"
                "4. Government tender announcements for CCTV projects\n\n"
                "Always cite your sources with URLs. Present data in structured tables."
            ),
            tools=[{"type": "web_search"}],
        )
        log.info(f"Created web search agent: {agent.id}")
        return agent
    except Exception as e:
        log.error(f"Failed to create web search agent: {e}")
        raise


def create_code_exec_agent(client: Mistral) -> object:
    """Creates a Mistral agent with code execution capability.
    
    Use case: BOQ calculations, cost rollups, quantity analysis,
    data transformations during bid preparation.
    
    Args:
        client: Initialized Mistral client with scale-as-you-go API key.
    
    Returns:
        Agent object with .id attribute for subsequent queries.
    """
    try:
        agent = client.agents.create(
            model="mistral-large-latest",
            name="CP Plus Bid Calculator Agent",
            description="Executes Python code for bid calculations, BOQ analysis, and cost estimation.",
            instructions=(
                "You are a bid calculation assistant for CP Plus. "
                "Use code execution to:\n"
                "1. Calculate BOQ (Bill of Quantities) totals and subtotals\n"
                "2. Perform cost analysis and margin calculations\n"
                "3. Generate comparison charts between products\n"
                "4. Validate quantity requirements against product packaging units\n\n"
                "Always show your calculations and provide formatted output tables."
            ),
            tools=[{"type": "code_interpreter"}],
        )
        log.info(f"Created code execution agent: {agent.id}")
        return agent
    except Exception as e:
        log.error(f"Failed to create code execution agent: {e}")
        raise


def query_agent(client: Mistral, agent_id: str, query: str) -> str:
    """Queries a Mistral agent and returns the response text.
    
    Args:
        client: Initialized Mistral client.
        agent_id: ID of the agent to query (from create_*_agent().id).
        query: Natural language query.
    
    Returns:
        Agent response as a string.
    """
    try:
        response = client.agents.complete(
            agent_id=agent_id,
            messages=[{"role": "user", "content": query}],
        )
        return response.choices[0].message.content
    except Exception as e:
        log.error(f"Agent query failed: {e}")
        return f"Agent error: {e}"
