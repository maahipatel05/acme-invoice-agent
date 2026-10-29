"""A scripted stand-in for Gemini so the agent loop is tested without network or key."""
import copy


class FakeModel:
    model = 'fake-model'

    def __init__(self, extractions=None, vp=None, critic=None, tool_calls=(('check_vendor', {'name': 'Widgets Inc.'}),)):
        self.extractions = list(extractions or [])   # successive extract() answers
        self.vp = list(vp or [{'decision': 'approve', 'reason': 'Clean invoice; vendor and stock verified'}])
        self.critic = list(critic or [{'verdict': 'agree', 'objections': [], 'assessment': 'Correct'}])
        self.tool_calls = tool_calls
        self.calls = 0
        self.usage = {'prompt_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0}
        self.feedback = []
        self.prompts = []
        self.tool_results = []

    def extract(self, text=None, data=None, feedback=None):
        self.calls += 1
        self.feedback.append(feedback)
        if not self.extractions:
            raise AssertionError('Unexpected extraction call')
        answer = self.extractions.pop(0) if len(self.extractions) > 1 else self.extractions[0]
        return copy.deepcopy(answer)

    def agent(self, instruction, prompt, tools, terminal, stage, max_turns=6, history=None):
        self.calls += 2
        self.prompts.append(prompt)
        for name, args in self.tool_calls:
            self.tool_results.append(tools[name][1](**args))
        answer = self.vp.pop(0) if len(self.vp) > 1 else self.vp[0]
        return copy.deepcopy(answer), (history or []) + [prompt]

    def structured(self, instruction, parts, schema, stage='structured_output'):
        self.calls += 1
        return copy.deepcopy(self.critic.pop(0) if len(self.critic) > 1 else self.critic[0])
