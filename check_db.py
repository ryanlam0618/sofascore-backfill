#!/usr/bin/env python3
import mysql.connector
import os
from dotenv import load_dotenv
load_dotenv('.env')

conn = mysql.connector.connect(
    host=os.getenv('MYSQL_HOST', '127.0.0.1'),
    port=int(os.getenv('MYSQL_PORT', '3306')),
    user=os.getenv('MYSQL_USER', 'root'),
    password=os.getenv('MYSQL_PASSWORD', ''),
    database=os.getenv('MYSQL_DATABASE', 'appdb'),
    charset='utf8mb4',
)
cur = conn.cursor()
for table in ['matches', 'match_incidents', 'match_lineups', 'match_statistics', 'match_shotmap', 'match_odds']:
    try:
        cur.execute(f'SELECT COUNT(*) FROM {table}')
        row = cur.fetchone()
        print(f'{table}: {row[0]}')
    except Exception as e:
        print(f'{table}: ERROR - {e}')
cur.close()
conn.close()