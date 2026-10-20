import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / '.python_packages'))

import asyncio
from agent_system.core.agent import run_agent

async def test():
    # 测试通知场景
    result = await run_agent('通知张三明天下午三点开会', user_id='test_user')
    print('Success:', result['success'])
    print('Error:', result['error'])
    print('Answer:', result['answer'][:200] if result['answer'] else None)
    print('Intent ID:', result['intent']['intent_id'] if result['intent'] else None)
    print('Steps:', len(result['steps']))
    for step in result['steps']:
        print('  Step %s: %s - %s' % (step['step_id'], step['tool_name'], step['status']))
    print('Latency:', result['latency_ms'], 'ms')
    print()

if __name__ == '__main__':
    asyncio.run(test())
