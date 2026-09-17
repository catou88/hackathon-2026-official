"""Run with: python agentstest/run.py --dataset ... --queries ... --out ..."""
import sys
sys.dont_write_bytecode = True
import os

if __name__ == "__main__":
    if os.environ.get('MINIRCA_WORKER_TOKEN'):
        from mini_rca.cli import main
    else:
        from mini_rca.supervision import main
    main()
