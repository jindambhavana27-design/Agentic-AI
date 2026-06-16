import re
from litellm import completion


MODEL = "ollama_chat/llama3.2"


def call_llm(messages):
    response = completion(
        model=MODEL,
        messages=messages,
        max_tokens=2000
    )
    return response.choices[0].message.content


def extract_code(text):
    pattern = r"```python(.*?)```"
    match = re.search(pattern, text, re.DOTALL)

    if match:
        return match.group(1).strip()

    return text.strip()


def save_to_file(filename, code):
    with open(filename, "w", encoding="utf-8") as file:
        file.write(code)


def main():
    user_requirement = input("What Python function do you want to create? ")

    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert Python developer. "
                "Always write clean Python code. "
                "Return only Python code inside a ```python code block."
            )
        }
    ]

    print("\nSTEP 1: Creating basic function...\n")

    prompt1 = {
        "role": "user",
        "content": f"""
Write a basic Python function for this requirement:

{user_requirement}

Return only the function code.
"""
    }

    messages.append(prompt1)
    response1 = call_llm(messages)
    print(response1)

    basic_code = extract_code(response1)

    messages.append({
        "role": "assistant",
        "content": response1
    })

    print("\nSTEP 2: Adding documentation...\n")

    prompt2 = {
        "role": "user",
        "content": f"""
Here is the Python function:

```python
{basic_code} 

Add documentation including:

Function description
Parameters
Return value
Example usage
Edge cases

Return only the documented Python code.
"""
}

    messages.append(prompt2)
    response2 = call_llm(messages)
    print(response2)

    documented_code = extract_code(response2)

    messages.append({
        "role": "assistant",
        "content": response2
    })

    print("\nSTEP 3: Adding unittest test cases...\n")

    prompt3 = {
    "role": "user",
    "content": f"""
    Here is the documented Python function:
    {documented_code}
    Add unittest test cases.

    Return a complete Python file containing:

    The documented function
    unittest test class

    if __name__ == "__main__": unittest.main()
    """
    }

    messages.append(prompt3)
    response3 = call_llm(messages)
    print(response3)

    final_code = extract_code(response3)

    save_to_file("generated_function_with_tests.py", final_code)

    print("\nFinal file saved as generated_function_with_tests.py")

if __name__ == "__main__":
   main()