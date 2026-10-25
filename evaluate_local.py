"""Run a small real local-LLM evaluation, preserving conversation history."""
import argparse
import asyncio
import builtins
from pathlib import Path

from local_client import main

PROMPTS = [
    'Make a two-step plan: first rotate only the base joint a little clockwise; second return the base joint to its initial angle.',
    'Repeat those two steps with half the angular amplitude, taking twice as long for each step.',
    'Make a two-step plan: first move the shoulder and elbow joints slightly at the same time; second return both joints to their initial angles.',
    'exit',
]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--model', default='qwen3:4b')
    parser.add_argument('--think', action='store_true')
    parser.add_argument('--log', type=Path, default=Path('artifacts/evaluation.jsonl'))
    args = parser.parse_args()
    prompts = iter(PROMPTS)

    def next_prompt(label):
        prompt = next(prompts)
        print(label + prompt, flush=True)
        return prompt

    builtins.input = next_prompt
    asyncio.run(main(argparse.Namespace(url='http://127.0.0.1:11434', model=args.model,
        docker=True, prompt=None, think=args.think, execute=args.execute, log=args.log)))
