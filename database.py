import aioodbc

# database config
server = 'umsdb.c7qyig0ucelj.ap-southeast-2.rds.amazonaws.com'
database = 'bleuIMS'
username = 'bleuadmin'
password = 'bleuadmin123'
driver = 'ODBC Driver 17 for SQL Server'

# async function to get db connection
async def get_db_connection():
    dsn = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
    )
    conn = await aioodbc.connect(dsn=dsn, autocommit=True)
    return conn
