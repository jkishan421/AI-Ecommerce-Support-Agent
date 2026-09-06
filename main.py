"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

app = BedrockAgentCoreApp()  # Replace this line

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-qwnv0g5wfe.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID = "MQ7IHJLR6N"
REGION = "us-east-1"
MEMORY_ID = "CustomerSupportMemory-4jdLNb2CTD"

# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)  # Replace this line

memory_client = MemoryClient(region_name=REGION)  # Replace this line

_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)  # Replace this line


SYSTEM_PROMPT = """
You are an AI customer support assistant for an e-commerce platform.
You help customers track orders, process returns, answer product and policy
questions, calculate loyalty discounts, and look up real-time information —
all in one conversation.

## Your tools and when to use them

- **Gateway tools (order/return/account functions):** Use these for anything
  involving a specific customer's orders, shipments, refunds, or account
  actions — e.g. "where is my order", "cancel this", "start a return". Always
  call the tool to get live data; never guess an order status or refund amount.
- **search_knowledge_base:** Use for general questions about product specs,
  return/warranty policies, loyalty program rules, or order-status
  definitions. This is retrieval-augmented — only state facts it returns.
  If it returns nothing relevant, say so rather than inventing an answer.
- **calculate_loyalty_discount:** Use this for ANY loyalty points or discount
  math (points redemption, tier discounts, final totals). Never compute these
  numbers yourself — the tool runs exact arithmetic in a sandbox. Ask the
  customer for their loyalty points, tier, order total, and product category
  if you don't already have them (check Customer Context first).
- **browser:** Use only when you need current information not covered by the
  knowledge base or gateway tools (e.g. a live external page the customer
  references, or something time-sensitive outside your other tools' scope).

## Customer Context

Some messages will be prefixed with a "Customer Context:" block containing
facts remembered from past sessions (preferences, prior issues, name, etc.).
Use this context to personalize your response and avoid re-asking for
information you already have — but never fabricate details beyond what's
given, and don't expose the raw context block or mention "memory" mechanics
to the customer.

## Behavior rules

1. Always use a tool to get order-specific, policy, or numeric information —
   never rely on your own assumptions for anything that could be wrong.
2. If a tool fails or returns an error, tell the customer plainly (e.g.
   "I'm having trouble pulling that up right now") and offer an alternative
   or to try again — don't pretend it succeeded.
3. Ask for missing required details (order ID, customer ID, etc.) before
   calling a tool that needs them, unless Customer Context already has them.
4. Keep responses concise, warm, and professional. Use plain language, not
   internal system or tool names.
5. For refunds, cancellations, or account changes, confirm the specific
   action and its consequence (e.g. "This will refund $X to your original
   payment method") before or as part of executing it.
6. If a request is outside what your tools can do (e.g. legal disputes,
   account security issues, or fraud claims), acknowledge it and recommend
   escalation to a human agent rather than guessing.
7. Never share other customers' data, internal system details, or raw error
   traces.
8. Stay strictly on e-commerce/customer-support topics; politely redirect
   off-topic requests.
"""


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
            self,
            actor_id: str,
            session_id: str,
            memory_client: MemoryClient,
            memory_id: str,
    ):
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.session_id = session_id
        self.actor_id = actor_id
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)
        logger.info("Namespaces loaded: %s", self.namespaces)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        # Steps:
        #   1. Get the last message from event.agent.messages
        #   2. Check it is a user message and not a tool result
        #   3. Extract the user query text
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        #   5. Collect non-empty memory texts with strategy type tags
        #   6. If any found, prepend them to the user message
        messages = event.agent.messages

        if (
                not messages
                or messages[-1]["role"] != "user"
                or "toolResult" in messages[-1]["content"][0]
        ):
            return

        user_query = messages[-1]["content"][0]["text"]

        try:
            all_context = []
            for strategy_type, namespace in self.namespaces.items():
                resolved_namespace = namespace.format(actorId=self.actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=resolved_namespace,
                    query=user_query,
                    top_k=5,
                )

                for memory in memories:
                    if isinstance(memory, dict):
                        text = memory.get("content", {}).get("text", "").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")

            if all_context:
                context_block = "\n".join(all_context)
                original_text = messages[-1]["content"][0]["text"]
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{context_block}\n\n{original_text}"
                )
                logger.info("Retrieved %d memory items for actor %s", len(all_context), self.actor_id)
        except Exception as e:
            logger.error("Failed to retrieve customer context: %s", e)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        # Steps:
        #   1. Get messages from event.agent.messages
        #   2. Walk backwards to find the last user query (plain text)
        #      and the last assistant response
        #   3. Call memory_client.create_event() with both messages
        try:
            messages = event.agent.messages
            agent_text = None
            user_text = None

            for msg in reversed(messages):
                if msg["role"] == "assistant" and not agent_text:
                    content = msg["content"]
                    if isinstance(content, list):
                        agent_text = content[0].get("text", "")
                    else:
                        agent_text = str(content)
                elif (
                        msg["role"] == "user"
                        and not user_text
                        and "toolResult" not in msg["content"][0]
                ):
                    user_text = msg["content"][0]["text"]
                    break

            if user_text and agent_text:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (user_text, "USER"),
                        (agent_text, "ASSISTANT"),
                    ],
                )
                logger.info("Saved interaction to memory for actor %s", self.actor_id)

        except Exception as e:
            logger.error("Failed to save interaction: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )
    results = resp.get("retrievalResults", [])
    if not results:
        return f"No information found for: {query}"

    chunks = [r["content"]["text"] for r in results]
    return "\n---\n".join(chunks)


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
        loyalty_points: int,
        tier: str,
        order_total: float,
        product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
    loyalty_points = {loyalty_points}
    tier = {tier!r}
    order_total = {order_total}
    product_category = {product_category!r}
    
    # Earn rates by product category
    earn_rates = {{
        "standard": 1,
        "device": 2,
        "fresh": 5,
    }}
    
    # Tier discount rates
    tier_rates = {{
        "Silver": 0.00,
        "Gold": 0.10,
        "Platinum": 0.15,
    }}
    
    # Points can only be redeemed in multiples of 500
    points_redeemed = (loyalty_points // 500) * 500
    
    # Points redemption cannot exceed 50% of the order total
    max_points_value = order_total * 0.50
    max_points_redeemed = int(max_points_value)
    
    # Maximum redemption must also be a multiple of 500
    max_points_redeemed = (max_points_redeemed // 500) * 500
    
    points_redeemed = min(points_redeemed, max_points_redeemed)
    
    # Each loyalty point is worth $0.01
    points_discount = points_redeemed * 0.01
    
    # Apply tier discount after points redemption
    subtotal_after_points = order_total - points_discount
    
    tier_discount_rate = tier_rates.get(tier, 0.00)
    tier_discount = subtotal_after_points * tier_discount_rate
    
    # Final calculations
    final_total = subtotal_after_points - tier_discount
    total_savings = points_discount + tier_discount
    
    # Points earned from this purchase
    points_earned = int(final_total * earn_rates.get(product_category, 1))
    
    # Remaining loyalty points
    remaining_points = loyalty_points - points_redeemed
    
    result = {{
        "tier": tier,
        "product_category": product_category,
        "order_total": round(order_total, 2),
        "points_redeemed": points_redeemed,
        "points_discount": round(points_discount, 2),
        "tier_discount": round(tier_discount, 2),
        "total_savings": round(total_savings, 2),
        "final_total": round(final_total, 2),
        "points_earned": points_earned,
        "remaining_points": remaining_points,
    }}
    
    print(result)
    """

    logger.info(f"\nGenerated Code:\n{code}\n")
    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke("executeCode", {
                "code": code,
                "language": "python",
                "clearContext": True,
            })

        for event in response["stream"]:
            return json.dumps(event["result"])


    except Exception as e:
        # Fallback: calculate tier discount only
        tier_rates = {
            "Silver": 0.00,
            "Gold": 0.10,
            "Platinum": 0.15,
        }

        tier_discount_rate = tier_rates.get(tier, 0.00)
        tier_discount = order_total * tier_discount_rate
        final_total = order_total - tier_discount

        fallback_result = {
            "tier": tier,
            "product_category": product_category,
            "order_total": round(order_total, 2),
            "points_redeemed": 0,
            "points_discount": 0.00,
            "tier_discount": round(tier_discount, 2),
            "total_savings": round(tier_discount, 2),
            "final_total": round(final_total, 2),
            "points_earned": 0,
            "remaining_points": loyalty_points,
            "fallback": True,
        }

        return json.dumps(fallback_result)

    # ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
    # Implement the invoke() function decorated with @app.entrypoint.
    #
    # Steps:
    #   1. Extract user_input, actor_id, and session_id from the payload
    #      (generate a UUID if session_id is missing)
    #   2. Instantiate MemoryHook for this actor/session
    #   3. Instantiate AgentCoreBrowser(region=REGION)
    #   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
    #                              agent_core_browser.browser]
    #   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
    #   6. Create and invoke the Agent with all tools, hooks, and system_prompt
    #   7. Return the text from the first content block of the response
    #   8. Handle exceptions gracefully


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    user_input = payload.get("prompt", "Hello!")
    session_id = payload.get("session_id", str(uuid.uuid4()))
    actor_id = payload.get("customer_id", "jai-user")

    memory_hook = MemoryHook(
        memory_client=memory_client,
        session_id=session_id,
        actor_id=actor_id,
        memory_id=MEMORY_ID
    )

    browser = AgentCoreBrowser(region=REGION)
    tools: list = [search_knowledge_base, calculate_loyalty_discount, browser.browser]

    client = MCPClient(
        lambda : streamable_http_client(url=GATEWAY_URL)
    )
    with client:
        gateway_tools = client.list_tools_sync()
        tools.extend(gateway_tools)

        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            state={"session_id": session_id, "actor_id": actor_id},
            hooks=[memory_hook],
        )

        response = agent(user_input)
    return response

# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    # parser = argparse.ArgumentParser()
    # parser.add_argument("payload", type=str)
    # args = parser.parse_args()
    #
    # response = asyncio.run(invoke(json.loads(args.payload)))

    payload = {
        "prompt": "Can you track order ORD-001?",
        "customer_id": "CUST-123",
        "session_id": "t1",
    }

    response = asyncio.run(invoke(payload))
    print(response)


if __name__ == "__main__":
    # app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    main()
