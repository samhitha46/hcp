import os

value = os.environ.get("TMP_TEST")
if value is None:
    print("TMP_TEST is not set")
else:
    print(f"TMP_TEST={value}")
