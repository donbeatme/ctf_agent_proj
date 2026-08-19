import os
p = r"maze_public"
data = open(p, "rb").read()
print("size:", len(data))
print("first 200 bytes repr:")
print(repr(data[:200]))
print("hex of first 96 bytes:", data[:96].hex())
