import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from app.core.database import Database
from app.core.config import get_config
import json

async def migrate():
    await Database.init()
    db = Database.get()
    
    # 列出所有用户
    async with db._db.execute('SELECT id, username, display_name FROM users ORDER BY created_at') as cursor:
        rows = [r async for r in cursor]
    
    print(f'Found {len(rows)} users:')
    for uid, uname, dname in rows:
        print(f'  - {uname} ({dname})')
    
    # 找 admin 用户
    admin_id = None
    for uid, uname, _ in rows:
        if uname == 'admin':
            admin_id = uid
            break
    
    if not admin_id:
        print('No admin user found, skipping migration')
        return
    
    # 读取旧配置
    home_config = get_config('home', {})
    if not home_config:
        print('No home config found in config.json')
        return
    
    home_data = {
        'home_name': home_config.get('home_name', ''),
        'owner_name': home_config.get('owner_name', ''),
        'province': home_config.get('province', ''),
        'city': home_config.get('city', ''),
        'district': home_config.get('district', ''),
    }
    
    print(f'\\nOld config: {json.dumps(home_data, ensure_ascii=False)}')
    
    # 检查是否已有
    existing = await db.user_setting_get(admin_id, 'home_info')
    if existing:
        print(f'Admin already has home_info: {existing}')
        print('Skipping migration')
    else:
        # 迁移
        await db.user_setting_set(admin_id, 'home_info', json.dumps(home_data, ensure_ascii=False))
        print(f'\\nMigrated home_info to admin user')
    
    await db._db.close()
    print('\\nMigration completed')

if __name__ == '__main__':
    asyncio.run(migrate())
