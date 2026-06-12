"""Mock LLM used for deployment labs without a real provider key."""
import random
import time


MOCK_RESPONSES = {
    "default": [
        "This is a mock AI response. In production this would come from a real LLM provider.",
        "Agent is running correctly. The request reached the production service.",
        "The production agent received your question and returned this mocked answer.",
    ],
    "docker": ["Docker packages an app and its dependencies so it runs consistently everywhere."],
    "deploy": ["Deployment is the process of moving code from local development to a reachable server."],
    "redis": ["Redis is useful for shared state such as sessions, rate limits, and usage counters."],
}


def ask(question: str, delay: float = 0.1) -> str:
    time.sleep(delay + random.uniform(0, 0.05))
    question_lower = question.lower()
    for keyword, responses in MOCK_RESPONSES.items():
        if keyword in question_lower:
            return random.choice(responses)
    return random.choice(MOCK_RESPONSES["default"])
