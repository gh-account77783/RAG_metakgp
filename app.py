import warnings

import streamlit as st

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

from RAG.got_engine import GoTReasoningEngine
from streamlit_agraph import agraph, Node, Edge, Config

st.set_page_config(
    page_title="GraphMind - MetaKGP Reasoning AI",
    page_icon="🧠",
    layout="wide"
)

st.markdown("""
    <style>
    .main {
        background-color: #f5f7f9;
    }
    .stChatMessage {
        border-radius: 15px;
        padding: 10px;
        margin-bottom: 10px;
    }
    </style>
    """, unsafe_allow_html=True)

def render_thought_graph(engine, path):
    nodes = []
    edges = []
    for index, url in enumerate(path):
        info = engine.neo4j.get_page_info(url)
        nodes.append(
            Node(
                id=url,
                label=info["title"] if info else url,
                size=20,
                color="#1e88e5",
            )
        )
        if index:
            edges.append(Edge(source=path[index - 1], target=url, label="EXPLORED"))

    agraph(
        nodes=nodes,
        edges=edges,
        config=Config(width=800, height=300, directed=True, physics=True),
    )


if "engine" not in st.session_state:
    with st.spinner("Initializing GraphMind Reasoning Engine..."):
        try:
            st.session_state.engine = GoTReasoningEngine()
        except Exception as e:
            st.error(f"Failed to initialize engine: {e}")
            st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("🧠 GraphMind")
st.markdown("### Advanced LLM Reasoning & Verification over MetaKGP")
st.info("This AI uses **Graph of Thoughts (GoT)** and **Mixture of Experts (MoE)** to provide verified answers strictly based on MetaKGP data.")

with st.sidebar:
    st.header("System Status")
    st.success("✅ LLM: Ollama Cloud")
    st.success("✅ Vector Store: ChromaDB")
    st.success("✅ Knowledge Graph: Neo4j")

    if st.button("Clear Chat History"):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if "details" in message:
            with st.expander("View Reasoning Trace"):
                st.write(message["details"])

if prompt := st.chat_input("Ask something about MetaKGP..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking through the graph..."):
            try:
                result = st.session_state.engine.reason(prompt)
                answer = result["answer"]
                path = result["path"]
                knowledge = result["knowledge"]
                verification = result.get("verification", {})

                st.markdown(answer)

                details = {
                    "Reasoning Path": path,
                    "MoE Verification": verification,
                    "Knowledge Set": knowledge
                }

                if path:
                    st.markdown("#### Thought Graph")
                    render_thought_graph(st.session_state.engine, path)

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer,
                    "details": details
                })

            except Exception as e:
                st.error(f"An error occurred during reasoning: {e}")
                st.session_state.messages.append({"role": "assistant", "content": f"Error: {e}"})
