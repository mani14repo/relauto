from .compiler import compile_catalog

if __name__ == "__main__":
    ts = compile_catalog()
    print(f"compiled {len(ts)} templates, {sum(len(t.criteria) for t in ts)} criteria")
