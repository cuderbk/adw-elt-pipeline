import pyodbc
import pandas as pd
import os
import pyarrow.parquet as pq
import pyarrow as pa
from sqlalchemy import create_engine
from dotenv import load_dotenv
import snowflake.connector


load_dotenv()
# --- Config ---
mssql_conn_str = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={os.getenv('SERVER_MSSQL')};"
    f"DATABASE={os.getenv('DATABASE_MSSQL')};"
    f"UID={os.getenv('UID_MSSQL')};"
    f"PWD={os.getenv('PWD_MSSQL')};"
    "Encrypt=yes;"
    "TrustServerCertificate=yes;"
)

# Snowflake configuration
snowflake_config = {
    "user": os.getenv("SNOWFLAKE_USER"),
    "password": os.getenv("SNOWFLAKE_PASSWORD"),
    "account": os.getenv("SNOWFLAKE_ACCOUNT"),
    "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
    "database": os.getenv("SNOWFLAKE_DATABASE"),
    "schema": os.getenv("SNOWFLAKE_SCHEMA"),
    "role": os.getenv("SNOWFLAKE_ROLE"),
}
output_dir = "./parquet_files"
os.makedirs(output_dir, exist_ok=True)

# --- Step 1: Connect to MSSQL ---
try:
    mssql_conn = pyodbc.connect(mssql_conn_str)
    print("Connected to MSSQL.")
    cursor = mssql_conn.cursor()
except pyodbc.Error as e:
    print(f"Failed to connect to MSSQL: {e}")
    exit(1)

# --- Step 2: Get table names ---
cursor.execute("""
    SELECT TABLE_SCHEMA, TABLE_NAME 
    FROM INFORMATION_SCHEMA.TABLES 
    WHERE TABLE_TYPE = 'BASE TABLE'
""")
tables = cursor.fetchall()

# # Print table names
for schema, table in tables:
    print(f"Schema: {schema}, Table: {table}")
# Uncomment to see all tables

# --- Step 3: Process each table ---
def mssql_to_sf_type(mssql_type):
    mapping = {
        'int': 'NUMBER',
        'bigint': 'NUMBER',
        'smallint': 'NUMBER',
        'tinyint': 'NUMBER',
        'bit': 'BOOLEAN',
        'varchar': 'VARCHAR',
        'nvarchar': 'VARCHAR',
        'char': 'VARCHAR',
        'nchar': 'VARCHAR',
        'text': 'VARCHAR',
        'ntext': 'VARCHAR',
        'datetime': 'TIMESTAMP_NTZ',
        'datetime2': 'TIMESTAMP_NTZ',
        'smalldatetime': 'TIMESTAMP_NTZ',
        'date': 'DATE',
        'time': 'TIME',
        'float': 'FLOAT',
        'real': 'FLOAT',
        'decimal': 'NUMBER',
        'numeric': 'NUMBER',
        'money': 'NUMBER',
        'smallmoney': 'NUMBER',
        'uniqueidentifier': 'VARCHAR',
        'xml': 'VARCHAR',
        'sql_variant': 'VARCHAR',
        'hierarchyid': 'VARCHAR',
        'geometry': 'VARCHAR',
        'geography': 'VARCHAR',
        'image': 'VARCHAR',
        'binary': 'VARCHAR',
        'varbinary': 'VARCHAR',
    }
    return mapping.get(mssql_type.lower(), 'VARCHAR')

def wrap_column_expr(col, dtype):
    dtype = dtype.lower()
    if dtype in ['image', 'varbinary', 'binary']:
        # Convert binary to hex string
        return f"CONVERT(VARCHAR(MAX), [{col}], 1) AS [{col}]"
    elif dtype in ['xml', 'sql_variant', 'hierarchyid', 'geometry', 'geography', 'text', 'ntext']:
        return f"CAST([{col}] AS NVARCHAR(MAX)) AS [{col}]"
    else:
        return f"[{col}]"


sf_ddl_statements = []

for schema, table in tables:
    print(f"🔄 Processing {schema}.{table}")

    # Get column metadata
    df_cols = pd.read_sql_query(f"""
        SELECT COLUMN_NAME, DATA_TYPE 
        FROM INFORMATION_SCHEMA.COLUMNS 
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}'
    """, mssql_conn)

    # Build Snowflake DDL
    column_defs = ",\n  ".join([
        f'"{row.COLUMN_NAME}" {mssql_to_sf_type(row.DATA_TYPE)}'
        for _, row in df_cols.iterrows()
    ])
    ddl = f'CREATE OR REPLACE TABLE {snowflake_config["schema"]}."STG_ADW_{table}" (\n  {column_defs}\n);'
    sf_ddl_statements.append(ddl)

    # Build SELECT query with type casting if needed
    unsupported_types = [
        'xml', 'sql_variant', 'hierarchyid', 'geometry', 'geography',
        'variant', 'image', 'text', 'ntext', 'binary', 'varbinary'
    ]
    col_exprs = [wrap_column_expr(row['COLUMN_NAME'], row['DATA_TYPE']) for _, row in df_cols.iterrows()]

    select_sql = f"SELECT {', '.join(col_exprs)} FROM [{schema}].[{table}]"

    # Export to Parquet
    df_data = pd.read_sql_query(select_sql, mssql_conn)
    if df_data.columns.duplicated().any():
        dup_cols = df_data.columns[df_data.columns.duplicated()].tolist()
        print(f"⚠️ Duplicate column names in {schema}.{table}: {dup_cols}")

    table_path = os.path.join(output_dir, f"{schema}_{table}.parquet")
    table_arrow = pa.Table.from_pandas(df_data)
    pq.write_table(table_arrow, table_path)

# --- Step 4: Upload to Snowflake ---
try:
    sf_conn = snowflake.connector.connect(**snowflake_config)
    sf_cursor = sf_conn.cursor()
    print("Connected to Snowflake.")
except snowflake.connector.Error as e:
    print(f"Failed to connect to Snowflake: {e}")
    exit(1)

for ddl in sf_ddl_statements:
    print(f"Executing DDL: {ddl}")
    sf_cursor.execute(ddl)

stage_name = f"@{snowflake_config['database']}.{snowflake_config['schema']}.staging_stage"

for schema, table in tables:
    parquet_file = f"{schema}_{table}.parquet"
    local_path = os.path.abspath(os.path.join(output_dir, parquet_file))

    full_table_name = f'{snowflake_config["database"]}.{snowflake_config["schema"]}."STG_ADW_{table}"'

    put_cmd = f"PUT file://{local_path} {stage_name}/ AUTO_COMPRESS=TRUE;"
    copy_cmd = f"""
        COPY INTO {full_table_name}
        FROM {stage_name}/{parquet_file}.gz
        FILE_FORMAT = (TYPE = PARQUET)
        MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;
    """

    try:
        print(f"📤 Uploading: {parquet_file} to {stage_name}")
        sf_cursor.execute(put_cmd)
    except Exception as e:
        print(f"❌ PUT failed for {parquet_file}: {e}")
        continue

    try:
        print(f"📥 Copying into Snowflake table: {full_table_name}")
        sf_cursor.execute(copy_cmd)
    except Exception as e:
        print(f"❌ COPY failed for {full_table_name}: {e}")



# Cleanup
cursor.close()
mssql_conn.close()
sf_cursor.close()
sf_conn.close()

print("✅ All done!")
