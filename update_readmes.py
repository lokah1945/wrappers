import os

wrappers = {
    'nous': {'port': 9102, 'upstream': 'Nous Research'},
    'opencode': {'port': 9103, 'upstream': 'OpenCode'},
    'blackbox': {'port': 9104, 'upstream': 'BLACKBOX AI'},
    'nvidia-python': {'port': 9101, 'upstream': 'NVIDIA NIM'}
}

for wrapper, info in wrappers.items():
    readme_path = f"{wrapper}/README.md"
    
    # Check if README exists and needs update
    if os.path.exists(readme_path):
        with open(readme_path, 'r') as f:
            content = f.read()
        
        # Add standardization notice if not present
        if "## Standardized Structure" not in content:
            # Find a good place to insert (after overview or at the beginning)
            if "# " in content:
                lines = content.split('\n')
                insert_idx = 0
                for i, line in enumerate(lines):
                    if line.startswith("# ") and "wrapper" in line.lower():
                        insert_idx = i + 1
                        break
                
                standardization_section = f"""
## Standardized Structure (2026-07-28)

This wrapper follows the standardized structure:

```
{wrapper}/
├── __init__.py
├── README.md
├── .env.example
├── src/
│   ├── __init__.py
│   └── main.py
└── systemd/ (optional)
```

### Run Command

```bash
# Development
uvicorn {wrapper.replace('-', '_')}.src.main:app --reload --port {info['port']}

# Production
uvicorn {wrapper.replace('-', '_')}.src.main:app --host 0.0.0.0 --port {info['port']} --workers 4
```

See WRAPPER_STANDARDIZATION_REPORT.md for details.

"""
                lines.insert(insert_idx, standardization_section)
                content = '\n'.join(lines)
                
                with open(readme_path, 'w') as f:
                    f.write(content)
                print(f"✅ Updated {readme_path}")
            else:
                print(f"⚠️  Could not find insertion point in {readme_path}")
        else:
            print(f"ℹ️  {readme_path} already has standardization section")
    else:
        print(f"❌ {readme_path} does not exist")

print("\n✅ README updates complete")
