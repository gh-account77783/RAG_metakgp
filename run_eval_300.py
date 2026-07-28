import pyarrow.dataset  # CRITICAL: Import pyarrow first to avoid DLL conflict and Access Violation crash on Windows
import os
import sys
import json
import random
import time
import re
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set up paths so we can import modules
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from RAG.got_engine import GoTReasoningEngine
from RAG.llm_client import LLMClient

# File paths
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CLEANED_WIKI_PATH = os.path.join(BASE_DIR, "Crawler", "cleaned_wiki.jsonl")
EVALUATION_ARTIFACT_DIR = os.path.join(BASE_DIR, "artifacts", "evaluation")
DATASET_PATH = os.path.join(EVALUATION_ARTIFACT_DIR, "eval_dataset_300.json")
PROGRESS_PATH = os.path.join(EVALUATION_ARTIFACT_DIR, "eval_progress_300.json")
REPORT_PATH = os.path.join(EVALUATION_ARTIFACT_DIR, "eval_report_300.json")

# Config
NUM_QUESTIONS = 100
MAX_GENERATION_WORKERS = 1
MAX_EVAL_WORKERS = 1

class EvaluationPipeline:
    def __init__(self):
        self.llm = LLMClient()
        self.engine = None  # Lazy-loaded for evaluation phase

    def generate_qa_pair(self, doc: Dict) -> Dict:
        """Generates a Q&A pair from a single wiki document."""
        time.sleep(1.0)
        url = doc.get("url")
        title = doc.get("title")
        content = doc.get("content", "")[:3000]  # Limit context size for prompt

        system_prompt = (
            "You are a factual question-generation expert. Given the title and content of a wiki page, "
            "generate exactly one specific, clear, factual question that can be answered using this page, "
            "and a direct, correct reference answer to it.\n"
            "Rules:\n"
            "- Do not mention the word 'context', 'wiki', or 'document' in the question.\n"
            "- Make the question natural as if asked by a student.\n"
            "- Respond strictly in valid JSON format with the keys 'question' and 'reference_answer'."
        )

        prompt = f"Title: {title}\nURL: {url}\n\nContent:\n{content}"

        for attempt in range(3):
            try:
                res_text = self.llm.generate(prompt, system_prompt=system_prompt)
                # Parse JSON
                match = re.search(r'\{.*\}', res_text, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    question = data.get("question")
                    reference_answer = data.get("reference_answer")
                    if question and reference_answer:
                        return {
                            "ground_truth_url": url,
                            "ground_truth_title": title,
                            "question": question,
                            "reference_answer": reference_answer
                        }
            except Exception as e:
                time.sleep(2)
        
        # Fallback if generation failed
        return {
            "ground_truth_url": url,
            "ground_truth_title": title,
            "question": f"What is the details of {title}?",
            "reference_answer": f"Details can be found on page: {content[:100]}..."
        }

    def build_dataset(self) -> List[Dict]:
        """Loads wiki docs, filters them, picks 300 random ones, and generates Q&A pairs."""
        os.makedirs(EVALUATION_ARTIFACT_DIR, exist_ok=True)
        if os.path.exists(DATASET_PATH):
            print(f"Loading existing test dataset from {DATASET_PATH}...")
            with open(DATASET_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

        print(f"Reading documents from {CLEANED_WIKI_PATH}...")
        docs = []
        with open(CLEANED_WIKI_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    # Filter for documents with substantial content
                    if len(data.get("content", "")) > 400:
                        docs.append(data)
                except Exception:
                    continue

        print(f"Found {len(docs)} documents with substantial content.")
        if len(docs) < NUM_QUESTIONS:
            selected_docs = docs
            print(f"Warning: Only {len(docs)} documents available. Using all of them.")
        else:
            random.seed(42)  # For reproducibility
            selected_docs = random.sample(docs, NUM_QUESTIONS)

        print(f"Generating {len(selected_docs)} Q&A pairs concurrently...")
        dataset = []
        
        with ThreadPoolExecutor(max_workers=MAX_GENERATION_WORKERS) as executor:
            futures = {executor.submit(self.generate_qa_pair, doc): doc for doc in selected_docs}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    qa = future.result()
                    dataset.append(qa)
                    if i % 10 == 0 or i == len(selected_docs):
                        print(f"Generated {i}/{len(selected_docs)} questions...")
                except Exception as e:
                    print(f"Error generating Q&A for a document: {e}")

        # Save to disk
        with open(DATASET_PATH, "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=4)
        print(f"Test dataset saved to {DATASET_PATH}!")
        return dataset

    def grade_answer(self, query: str, generated_answer: str, reference_answer: str) -> Dict:
        """Uses LLM-as-a-judge to score correctness of the response from 0.0 to 5.0."""
        system_prompt = (
            "You are an expert evaluation judge. You compare a generated RAG answer against a reference ground truth "
            "answer to determine accuracy. Score the generated answer on a scale from 0.0 to 5.0.\n"
            "Criteria:\n"
            "- 5.0: The generated answer is fully correct and covers all key facts in the reference answer.\n"
            "- 3.0 - 4.9: Mostly correct, but misses minor details or contains minor irrelevant context.\n"
            "- 1.0 - 2.9: Has significant inaccuracies or fails to cover major parts of the reference answer.\n"
            "- 0.0: Totally incorrect, hallucinated, or states 'I don't know' when reference answer has facts.\n\n"
            "Respond ONLY with a JSON object containing the float 'score' and a string 'reason' explaining your grade."
        )
        
        prompt = f"""
        Query: {query}
        Reference Answer: {reference_answer}
        Generated Answer: {generated_answer}
        
        Provide the score and reasoning in JSON format:
        {{
            "score": <float>,
            "reason": "<string>"
        }}
        """
        for attempt in range(3):
            try:
                res_text = self.llm.generate(prompt, system_prompt=system_prompt)
                match = re.search(r'\{.*\}', res_text, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    return {
                        "score": float(data.get("score", 0.0)),
                        "reason": data.get("reason", "No reason provided.")
                    }
            except Exception:
                time.sleep(2)
        return {"score": 0.0, "reason": "Failed to evaluate via LLM Grader."}

    def evaluate_case(self, case: Dict) -> Dict:
        """Evaluates a single Q&A case through RAG and grades it."""
        time.sleep(1.0)
        query = case["question"]
        ref_ans = case["reference_answer"]
        gt_url = case["ground_truth_url"]

        t0 = time.time()
        try:
            # Run query through RAG engine
            res = self.engine.reason(query)
            duration = time.time() - t0

            generated_ans = res["answer"]
            path = res["path"]
            verification = res["verification"]

            # Calculate recall (hit rate)
            hit = gt_url in path

            # Grade correctness
            grade_res = self.grade_answer(query, generated_ans, ref_ans)

            return {
                "question": query,
                "ground_truth_url": gt_url,
                "reference_answer": ref_ans,
                "generated_answer": generated_ans,
                "thought_path": path,
                "moe_verification": verification,
                "hit": hit,
                "score": grade_res["score"],
                "reasoning": grade_res["reason"],
                "duration_seconds": duration,
                "status": "success"
            }
        except Exception as e:
            return {
                "question": query,
                "ground_truth_url": gt_url,
                "reference_answer": ref_ans,
                "status": "failed",
                "error": str(e)
            }

    def run_evaluation(self, dataset: List[Dict]):
        """Runs evaluation over the dataset and saves progress incrementally."""
        dataset = dataset[:NUM_QUESTIONS]
        print("Initializing GoTReasoningEngine...")
        self.engine = GoTReasoningEngine()

        # Load completed progress
        completed = {}
        if os.path.exists(PROGRESS_PATH):
            try:
                with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                    completed_list = json.load(f)
                    completed = {c["question"]: c for c in completed_list if c.get("status") == "success"}
                print(f"Loaded completed progress: {len(completed)}/{len(dataset)} items already processed.")
            except Exception:
                print("Could not load progress file. Starting fresh.")

        remaining_cases = [c for c in dataset if c["question"] not in completed]
        print(f"Remaining cases to process: {len(remaining_cases)}")

        results = list(completed.values())

        if remaining_cases:
            print(f"Running end-to-end evaluation with {MAX_EVAL_WORKERS} concurrent threads...")
            # We save progress after every single completion to be 100% resilient
            with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS) as executor:
                futures = {executor.submit(self.evaluate_case, case): case for case in remaining_cases}
                for i, future in enumerate(as_completed(futures), 1):
                    eval_res = future.result()
                    if eval_res.get("status") == "success":
                        results.append(eval_res)
                        # Save progress incrementally
                        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
                            json.dump(results, f, indent=4)
                        print(f"[{i + len(completed)}/{len(dataset)}] Evaluated: '{eval_res['question']}' -> Score: {eval_res['score']}/5.0 (Recall: {eval_res['hit']})")
                    else:
                        print(f"Error evaluating query '{eval_res['question']}': {eval_res.get('error')}")

        # Compute aggregate metrics
        total_eval = len(results)
        if total_eval == 0:
            print("No cases successfully evaluated.")
            return

        hits = sum(1 for r in results if r.get("hit", False))
        recall = (hits / total_eval) * 100
        avg_score = sum(r.get("score", 0.0) for r in results) / total_eval
        avg_accuracy = (avg_score / 5.0) * 100
        avg_time = sum(r.get("duration_seconds", 0.0) for r in results) / total_eval

        summary = {
            "total_queries": total_eval,
            "retrieval_hits": hits,
            "recall_percentage": recall,
            "average_grader_score": avg_score,
            "average_accuracy_percentage": avg_accuracy,
            "average_query_duration_seconds": avg_time
        }

        print("\n================ EVALUATION COMPLETED ================")
        print(f"Total Evaluated Queries:   {summary['total_queries']}")
        print(f"Retrieval Recall (Hit Rate): {summary['recall_percentage']:.2f}%")
        print(f"Average Grader Score (0-5):  {summary['average_grader_score']:.2f}")
        print(f"Average Accuracy Score:      {summary['average_accuracy_percentage']:.2f}%")
        print(f"Average Response Time:       {summary['average_query_duration_seconds']:.2f} seconds")
        print("======================================================")

        # Write final report
        report = {
            "summary": summary,
            "details": results
        }
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4)
        print(f"Final evaluation report saved to {REPORT_PATH}!")

        # Generate a markdown report
        md_content = f"""# 📈 GraphMind RAG 300-Query Evaluation Report

This report summarizes the end-to-end evaluation of the GraphMind reasoning engine on 300 randomly generated questions mapped to their ground-truth MetaKGP pages.

## 📊 Summary Metrics

| Metric | Value |
| :--- | :--- |
| **Total Test Queries** | {summary['total_queries']} |
| **Retrieval Hits** | {summary['retrieval_hits']} / {summary['total_queries']} |
| **Retrieval Recall (Hit Rate)** | **{summary['recall_percentage']:.2f}%** |
| **Average Grader Score** | **{summary['average_grader_score']:.2f} / 5.0** |
| **Average Accuracy %** | **{summary['average_accuracy_percentage']:.2f}%** |
| **Average Response Time** | **{summary['average_query_duration_seconds']:.2f}s** |

## 🔍 Key Findings & Analysis
*   **Recall (Hit Rate)** indicates whether the Graph of Thoughts (GoT) engine traversed and visited the correct page.
*   **Accuracy Score** reflects the model's factual coverage compared to the reference answers, graded by the AI judge.

Detailed results are stored in `eval_report_300.json`.
"""
        summary_path = os.path.join(EVALUATION_ARTIFACT_DIR, "evaluation_summary_report.md")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Summary report written to {summary_path}")

if __name__ == "__main__":
    pipeline = EvaluationPipeline()
    try:
        # Step 1: Generate dataset
        dataset = pipeline.build_dataset()
        # Step 2: Run evaluations
        pipeline.run_evaluation(dataset)
    finally:
        if pipeline.engine:
            pipeline.engine.close()
