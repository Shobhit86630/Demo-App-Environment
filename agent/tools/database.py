import os

import psycopg2


def inspect_database() -> dict:
    try:
        connection = psycopg2.connect(
            host="localhost",
            port=5433,
            database="sentinel",
            user="sentinel",
            password="sentinel",
            connect_timeout=5,
        )

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                count(*) AS total_connections
            FROM pg_stat_activity;
            """
        )

        connections = cursor.fetchone()[0]

        cursor.close()
        connection.close()

        return {
            "success": True,
            "database": "sentinel",
            "connections": connections,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }