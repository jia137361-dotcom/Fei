#!/usr/bin/env python3
"""Clear all task residues from database."""
import sys
sys.path.insert(0, "C:\\nanobot\\pinterest-pod-agent")

from sqlalchemy import create_engine, text
from app.config import get_settings

e = create_engine(get_settings().database_url)
with e.connect() as c:
    # Count before
    tasks = c.execute(text("SELECT COUNT(*) FROM scheduled_task")).scalar()
    jobs = c.execute(text("SELECT COUNT(*) FROM publish_job")).scalar()
    print(f"Before: {tasks} tasks, {jobs} jobs")

    # Delete all scheduled tasks
    c.execute(text("DELETE FROM scheduled_task"))
    
    # Delete all publish jobs
    c.execute(text("DELETE FROM publish_job"))
    
    c.commit()
    
    # Count after
    tasks = c.execute(text("SELECT COUNT(*) FROM scheduled_task")).scalar()
    jobs = c.execute(text("SELECT COUNT(*) FROM publish_job")).scalar()
    print(f"After: {tasks} tasks, {jobs} jobs")

print("All task residues cleared.")
