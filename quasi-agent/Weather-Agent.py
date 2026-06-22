from litellm import completion
import requests


MODEL = "ollama_chat/llama3.2"


def call_llm(messages):
    response = completion(
        model=MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0
    )
    return response.choices[0].message.content.strip()


def weather_tool(action):
    
    """
    This simulates calling the real Weather Tool.
    In real agents, this could be an API call to a weather service.
    """

    url = "https://api.openweathermap.org/data/2.5/weather"

    params = {
    "q": "Austin",
    "appid": "ec4beb159807151cccb0a1ad3ec88eb2"
            }

    response = requests.get(url, params=params)

    print(response.json())
    return f"Completed action: {action}"


def main():
    goal = input("Enter your goal: ")

    memory = []

    messages = [
        {
            "role": "system",
            "content": """
You are a Agent which calls the real Weather Tool.

Given the user's goal and memory of completed actions,
decide the next action.

Rules:
- Return only one short action.
- If the task is complete, return exactly: DONE
- Do not return JSON.
- Do not explain.
"""
        },
        {
            "role": "user",
            "content": f"Goal: {goal}\nCompleted actions: None\nWhat is the next action?"
        }
    ]

    max_steps = 10

    for step in range(max_steps):
        print(f"\n--- Agent Loop Step {step + 1} ---")

        next_action = call_llm(messages)

        print("LLM decided next action:")
        print(next_action)

        if next_action.upper() == "DONE":
            print("\nAgent says task is complete.")
            break

        result = weather_tool(next_action)

        print("Tool result:")
        print(result)

        memory.append(result)

        messages.append({
            "role": "assistant",
            "content": next_action
        })

        messages.append({
            "role": "user",
            "content": f"""
Goal: {goal}

Completed actions:
{chr(10).join(memory)}

Based on this memory, what is the next action?
"""
        })

    print("\nFinal Memory:")
    for item in memory:
        print("-", item)


if __name__ == "__main__":
    main()