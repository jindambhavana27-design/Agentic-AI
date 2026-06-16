import json
from litellm import completion


MODEL = "ollama_chat/llama3.2"


def calculator(expression: str) -> str:
    """
    Tool: Calculates a math expression.
    """
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


def call_llm(messages):
    """
    Sends messages to the LLM and returns the response text.
    """
    response = completion(
        model=MODEL,
        messages=messages,
        max_tokens=1000
    )
    return response.choices[0].message.content


def extract_json(text):
    text = text.strip()

    decoder = json.JSONDecoder()

    try:
        obj, index = decoder.raw_decode(text)
        return obj
    except json.JSONDecodeError:
        return {
            "action": "final_answer",
            "answer": "I could not understand the model response."
        }

def main():
    user_input = input("Ask me a math question: ")

    messages = [
        {
            "role": "system",
            "content": """
You are a simple AI agent.

You can use one tool:

Tool name: calculator
Tool purpose: Solves math expressions like 25 * 18 or 100 / 4.

You must respond ONLY in JSON.
Do not include markdown.
Do not include explanations.
Always close all braces in the Json Response.

If you need to use the calculator tool, respond:
{
  "action": "calculator",
  "expression": "math expression"
}

If you are ready to answer the user, respond:
{
  "action": "final_answer",
  "answer": "final answer to user"
}
dont miss the } in the7 end for json response you give
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
        print("\nLLM Response:")
        print(llm_response)

        action_data = extract_json(llm_response)
        print("\nParsed Action Data:")
        print(action_data)

        action = action_data.get("action")

        if action == "calculator":
            expression = action_data.get("expression")

            print("\nAgent decided to use calculator tool.")
            print("Expression:", expression)

            tool_result = calculator(expression)

            print("Tool Result:", tool_result)

            messages.append({
                "role": "assistant",
                "content": llm_response
            })

            messages.append({
                "role": "user",
                "content": f"The calculator result for {expression} is {tool_result}. What should be the final answer?"
            })

        elif action == "final_answer":
            print("\nFinal Answer:")
            print(action_data.get("answer"))
            break

        else:
            print("Unknown action. Stopping agent.")
            break


if __name__ == "__main__":
    main()