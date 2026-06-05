import os
import sys

# Добавляем путь к проекту в PYTHONPATH
sys.path.append(os.path.abspath("."))

from leasing_analyzer.services.search import search_google, filter_search_results, filter_irrelevant_results, _is_noisy_search_result

def main():
    query = "авито man tgs в лизинг"
    model_name = "man tgs"
    print("=== Organic Search ===")
    results = search_google(query, num_results=15)
    
    # Check all URLs before filter
    for r in results:
        is_noisy = _is_noisy_search_result(r, model_name)
        print(f"[RAW] {r.get('link')} | noisy={is_noisy}")

    filtered_irrelevant = filter_irrelevant_results(results)
    filtered_results = filter_search_results(filtered_irrelevant, model_name)
            
    print("\n=== Passed ===")
    for u in filtered_results:
        print(f"[PASS] {u.get('link')} | {u.get('title')}")

if __name__ == "__main__":
    main()
