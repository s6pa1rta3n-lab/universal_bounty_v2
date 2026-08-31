import json

urls = set()
with open("logs/intake_queue.jsonl") as f:
    for line in f:
        if line.strip():
            d = json.loads(line)
            if d.get("pr_url"):
                urls.add(d["pr_url"])

for u in list(urls)[:10]:
    print(u)
