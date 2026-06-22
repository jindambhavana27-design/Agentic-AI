import json
from pyexpat.errors import messages
from litellm import completion


MODEL = "ollama_chat/llama3.2"


# -------------------------------
# Developer-written tools/functions
# -------------------------------

def search_transactions(account_id: str, days: int = 30, limit: int = 4):
    """
    Tool 1: Search recent transactions.
    In real banking app, this would query DB.
    """
    transactions = [
        {"id": "TXN1001", "merchant": "Walmart", "amount": 45.20, "date": "2026-06-20"},
        {"id": "TXN1002", "merchant": "Amazon", "amount": 89.99, "date": "2026-06-18"},
        {"id": "TXN1004", "merchant": "Target", "amount": 32.10, "date": "2026-06-15"},
        {"id": "TXN1005", "merchant": "Costco", "amount": 120.00, "date": "2026-06-12"},
    ]
    limit = int(limit);

    return transactions[:limit]


def compare_transactions(merchant: str, amount: float | None = None):
    """
    Tool 2: Compare transactions to detect duplicates.
    """
    transactions = search_transactions("ACC123", days=30, limit=10)

    matches = []

    for txn in transactions:
        if txn["merchant"].lower() == merchant.lower():
            if amount is None or txn["amount"] == amount:
                matches.append(txn)

    if len(matches) >= 2:
        return {
            "duplicate_found": True,
            "matches": matches
        }

    return {
        "duplicate_found": False,
        "matches": matches
    }


def create_dispute(transaction_id: str, reason: str):
    """
    Tool 3: Create a dispute.
    """
    return {
        "status": "created",
        "dispute_id": "DSP-9001",
        "transaction_id": transaction_id,
        "reason": reason
    }


def notify_customer(message: str):
    """
    Tool 4: Notify customer.
    """
    return {
        "status": "sent",
        "message": message
    }


# -------------------------------
# LLM call
# -------------------------------

def call_llm(messages):
    response = completion(
        model=MODEL,
        messages=messages,
        max_tokens=700,
        temperature=0
    )
    return response.choices[0].message.content.strip()


# -------------------------------
# Robust JSON extraction
# -------------------------------

def extract_json(text):
    """
    Extract first JSON object from LLM response.
    Local LLMs sometimes add extra text, so this makes parsing safer.
    """
    text = text.strip()

    decoder = json.JSONDecoder()

    try:
        obj, _ = decoder.raw_decode(text)
        return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return {
        "function": "final_answer",
        "arguments": {
            "answer": "I could not understand the model response."
        }
    }


# -------------------------------
# Function dispatcher
# -------------------------------

def execute_function(function_name, arguments):
    """
    This maps LLM-selected function name to actual Python function.
    """

    if function_name == "search_transactions":
        return search_transactions(
            account_id=arguments.get("account_id", "ACC123"),
            days=arguments.get("days", 30),
            limit=arguments.get("limit", 4)
        )

    if function_name == "compare_transactions":
        return compare_transactions(
            merchant=arguments.get("merchant"),
            amount=arguments.get("amount")
        )

    if function_name == "create_dispute":
        return create_dispute(
            transaction_id=arguments.get("transaction_id"),
            reason=arguments.get("reason")
        )

    if function_name == "notify_customer":
        return notify_customer(
            message=arguments.get("message")
        )

    return {
        "error": f"Unknown function: {function_name}"
    }


# -------------------------------
# Main agentic function-calling loop
# -------------------------------

def main():
    user_input = input("Ask banking agent: ")

    messages = [
        {
            "role": "system",
            "content": """
You are a banking AI agent.

You have access to these functions:

1. search_transactions(account_id, days, limit)
Use this when user asks for recent transactions or transaction history.

2. compare_transactions(merchant, amount)
Use this when user asks about duplicate charges.

3. create_dispute(transaction_id, reason)
Use this when duplicate charge is confirmed and user wants a dispute.

4. notify_customer(message)
Use this to notify the user after an action.

Return exactly ONE JSON object.

If you need to call a function, return:
{
  "function": "function_name",
  "arguments": {
    "key": "value"
  }
}

If you are ready to answer user, return:
{
  "function": "final_answer",
  "arguments": {
    "answer": "your final answer"
  }
}

Do not include markdown.
Do not include explanations outside JSON.
"""
        },
        {
            "role": "user",
            "content": user_input
        }
    ]

    max_steps = 5

    for step in range(max_steps):
        print(f"\n--- Agent Loop Step {step + 1} ---")

    
        llm_response = call_llm(messages)

        print("\nLLM selected:")
        print(llm_response)

        decision = extract_json(llm_response)

        function_name = decision.get("function")
        arguments = decision.get("arguments", {})

        print("\nParsed function call:")
        print("Function:", function_name)
        print("Arguments:", arguments)

        if function_name == "final_answer":
            print("\nFinal Answer:")
            print(arguments.get("answer"))
            break

        print("\nExecuting function...")
        tool_result = execute_function(function_name, arguments)

        print("\nFunction Result:")
        print(tool_result)

        messages.append({
            "role": "assistant",
            "content": llm_response
        })

        messages.append({
            "role": "user",
            "content": f"""
Function {function_name} returned this result:

{tool_result}

Based on this result, decide the next function call or provide final_answer.
"""
        })


if __name__ == "__main__":
    main()