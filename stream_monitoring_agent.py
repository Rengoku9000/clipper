"""Report which streams a clipping agent is currently watching."""

import time


FOOTAGE_TIMEOUT = 30
HISTORY_LIMIT = 20


class StreamMonitoringAgent:
    def __init__(self, clipping, db):
        self.clipping = clipping
        self.db = db

    def snapshot(self):
        with self.clipping.lock:
            now = time.monotonic()
            streams = []
            active_events = set()
            for session in self.clipping.sessions.values():
                if session["rehearsal"] or session["process"].poll() is not None:
                    continue
                status = session["status"]
                if status == "Capturing":
                    status = "Watching" if now - session["last_footage"] <= FOOTAGE_TIMEOUT else "No recent video"
                streams.append({"channel": session["channel"], "started": session["time"], "status": status})
                active_events.add(session["start_event"])

            with self.db() as db:
                rows = db.execute("""
                    WITH lifecycle AS (
                        SELECT id, channel, time, kind, data,
                               LEAD(time) OVER session AS next_time,
                               LEAD(kind) OVER session AS next_kind
                        FROM events WHERE kind IN ('started', 'stopped', 'capture_failed')
                            AND COALESCE(json_extract(data, '$.platform'), 'twitch') = ?
                        WINDOW session AS (PARTITION BY channel ORDER BY id)
                    )
                    SELECT id, channel, time AS started, next_time, next_kind
                    FROM lifecycle
                    WHERE kind = 'started' AND COALESCE(json_extract(data, '$.rehearsal'), 0) = 0
                    ORDER BY id DESC LIMIT ?
                """, (self.clipping.platform, HISTORY_LIMIT + len(active_events))).fetchall()

        history = []
        for row in rows:
            if row["id"] in active_events:
                continue
            status = {"stopped": "Ended", "capture_failed": "Failed"}.get(row["next_kind"], "Interrupted")
            history.append({"channel": row["channel"], "started": row["started"], "status": status,
                            "ended": row["next_time"] if status != "Interrupted" else None})
        return {"active": sum(stream["status"] == "Watching" for stream in streams),
                "streams": streams, "history": history[:HISTORY_LIMIT]}
