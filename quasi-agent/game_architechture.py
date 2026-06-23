class SimpleGAMEAgent:
    def __init__(self):
        # G: Goal
        self.goal = "Help user manage tasks"

        # A: Actions
        self.actions = {
            "add_task": self.add_task,
            "list_tasks": self.list_tasks,
            "complete_task": self.complete_task
        }

        # M: Memory
        self.memory = {
            "tasks": []
        }

        # E: Environment
        self.environment = {
            "user_input": None
        }

    def add_task(self, task):
        self.memory["tasks"].append({
            "task": task,
            "completed": False
        })
        return f"Task added: {task}"

    def list_tasks(self):
        if not self.memory["tasks"]:
            return "No tasks found."

        result = []
        for i, task in enumerate(self.memory["tasks"], start=1):
            status = "Done" if task["completed"] else "Pending"
            result.append(f"{i}. {task['task']} - {status}")

        return "\n".join(result)

    def complete_task(self, task_number):
        index = int(task_number) - 1

        if 0 <= index < len(self.memory["tasks"]):
            self.memory["tasks"][index]["completed"] = True
            return f"Completed: {self.memory['tasks'][index]['task']}"

        return "Invalid task number."

    def decide_action(self, user_input):
        self.environment["user_input"] = user_input

        if user_input.startswith("add "):
            return "add_task", user_input.replace("add ", "")

        if user_input == "list":
            return "list_tasks", None

        if user_input.startswith("complete "):
            return "complete_task", user_input.replace("complete ", "")

        return None, None

    def run(self):
        print("Simple GAME Agent started.")
        print("Commands: add <task>, list, complete <number>, exit")

        while True:
            user_input = input("\nYou: ")

            if user_input == "exit":
                print("Agent stopped.")
                break

            action_name, value = self.decide_action(user_input)

            if action_name is None:
                print("Agent: I don't understand that command.")
                continue

            action = self.actions[action_name]

            if value is None:
                result = action()
            else:
                result = action(value)

            print("Agent:", result)


if __name__ == "__main__":
    agent = SimpleGAMEAgent()
    agent.run()