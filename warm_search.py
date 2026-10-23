"""Optional: pre-download model and prebuild KB index before running Discord bot.

Run from this folder after setting .env: python3 warm_search.py
The embedding model download is public; private repo documents stay on the host.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from kb import KnowledgeBase

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

async def main():
    kb = KnowledgeBase(
        folder=ROOT / 'data' / 'fsae-kb',
        repo_url=os.getenv('REPO_URL', 'git@github.com:Mean-Green-Racing/fsae-kb.git'),
        branch=os.getenv('REPO_BRANCH', 'main'),
        ssh_key_path=os.getenv('GITHUB_DEPLOY_KEY_PATH', ''),
        refresh_seconds=int(os.getenv('SYNC_INTERVAL_SECONDS', '600')),
        semantic_enabled=os.getenv('SEMANTIC_SEARCH', '1').lower() in ('1', 'true', 'yes'),
        embedding_threads=int(os.getenv('EMBEDDING_THREADS', '1')),
        embedding_batch_size=int(os.getenv('EMBEDDING_BATCH_SIZE', '8')),
    )
    await kb.ensure_ready()
    if kb._semantic_task is not None:
        await kb._semantic_task
    print(f'Knowledge base: {kb.file_count} files; commit: {kb.commit[:10]}')
    print(f'Semantic search: {kb.semantic_status}')
    if kb.semantic_last_error:
        print(f'Problem: {kb.semantic_last_error}')
    for question in ('Why did Mean Green Racing shut down?',
                     'What did we learn from F26 competition?'):
        print(f'\nQuery: {question}')
        for hit in await kb.search_async(question):
            print(' -', hit.passage.path, hit.passage.location)

if __name__ == '__main__':
    asyncio.run(main())
