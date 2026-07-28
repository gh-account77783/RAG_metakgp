import os
from RAG.llm_client import LLMClient

class MoEVerifier:
    def __init__(self):
        self.llm = LLMClient()

    def verify_source_match(self, claim, context):
        """Expert 1: Does the retrieved text actually support this claim?"""
        system_prompt = "You are a Source Matching Expert. Your only job is to verify if a specific claim is explicitly supported by the provided context."
        prompt = f"""
        Context:
        {context}

        Claim:
        {claim}

        Does the context explicitly support the claim?
        Respond with 'VERIFIED' or 'NOT_VERIFIED' followed by a brief reason.
        """
        response = self.llm.generate(prompt, system_prompt=system_prompt)
        return response

    def verify_hallucination(self, answer, context):
        """Expert 2: Is the bot inventing details not present in the scraped context?"""
        system_prompt = "You are a Hallucination Hunter. Your goal is to identify any information in the answer that is NOT found in the provided context."
        prompt = f"""
        Context:
        {context}

        Answer:
        {answer}

        Identify any facts in the answer that are NOT present in the context.
        If the answer is fully supported, respond 'CLEAN'.
        Otherwise, list the hallucinated details.
        """
        response = self.llm.generate(prompt, system_prompt=system_prompt)
        return response

    def verify_logic(self, answer, knowledge_set):
        """Expert 3: Does the conclusion follow logically from the premises?"""
        system_prompt = "You are a Logic Expert. You verify if the final conclusion follows logically from the extracted facts."
        prompt = f"""
        Knowledge Set (Premises):
        {knowledge_set}

        Final Answer:
        {answer}

        Does the final answer follow logically from the premises? Are there any logical leaps or contradictions?
        Respond with 'LOGICAL' or 'ILLOGICAL' followed by an explanation.
        """
        response = self.llm.generate(prompt, system_prompt=system_prompt)
        return response

    def orchestrate(self, answer, context, knowledge_set):
        """Judge that aggregates expert scores to decide if the answer is acceptable."""
        source_match = self.verify_source_match(answer, context)
        hallucination = self.verify_hallucination(answer, context)
        logic = self.verify_logic(answer, knowledge_set)

        system_prompt = "You are the Verification Judge. You aggregate findings from three experts to decide if an answer is trustworthy."
        prompt = f"""
        Answer: {answer}

        Expert 1 (Source Matcher): {source_match}
        Expert 2 (Hallucination Hunter): {hallucination}
        Expert 3 (Logic Expert): {logic}

        Based on these expert reviews, should the answer be accepted?
        Respond with 'ACCEPT' or 'REJECT'.
        If 'REJECT', explain why and what needs to be fixed.
        """
        decision = self.llm.generate(prompt, system_prompt=system_prompt)

        return {
            "decision": decision,
            "details": {
                "source_match": source_match,
                "hallucination": hallucination,
                "logic": logic
            }
        }

if __name__ == "__main__":
    # Simple test
    verifier = MoEVerifier()
    context = "The Technology Literary Society was founded in 1995 by a group of students."
    answer = "The TLS was founded in 1995."

    print("Testing Source Matcher...")
    print(verifier.verify_source_match(answer, context))

    print("\nTesting Hallucination Hunter...")
    print(verifier.verify_hallucination(answer, context))

    print("\nTesting Logic Expert...")
    print(verifier.verify_logic(answer, context))

    print("\nTesting Orchestrator...")
    print(verifier.orchestrate(answer, context, context))
