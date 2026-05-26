import streamlit as st
from got_engine import GoTReasoningEngine
import networkx as nx
from streamlit_agraph import agraph, Node, Edge

# Page configuration
st.set_page_config(
    page_title="GraphMind - MetaKGP Reasoning AI",
    page_icon="🧠",
    layout="wide"
)

# Custom CSS for better styling
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
    .citation-link {
        color: #1e88e5;
        text-decoration: none;
        font-size: 0.8em;
        margin-left: 5px;
    }
    </style>
    """, unsafe_allow_html=True)

# Initialize Reasoning Engine in session state
if "engine" not in st.session_state:
    with st.spinner("Initializing GraphMind Reasoning Engine..."):
        try:
            st.session_state.engine = GoTReasoningEngine()
        except Exception as e:
            st.error(f"Failed to initialize engine: {e}")
            st.stop()

# Chat History
if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("🧠 GraphMind")
st.markdown("### Advanced LLM Reasoning & Verification over MetaKGP")
st.info("This AI uses **Graph of Thoughts (GoT)** and **Mixture of Experts (MoE)** to provide verified answers strictly based on MetaKGP data.")

# Sidebar for system status and settings
with st.sidebar:
    st.header("System Status")
    st.success("✅ LLM: Ollama Cloud")
    st.success("✅ Vector Store: ChromaDB")
    st.success("✅ Knowledge Graph: Neo4j")

    if st.button("Clear Chat History"):
        st.session_state.messages = []
        st.rerun()

# Display chat history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if "details" in message:
            with st.expander("View Reasoning Trace"):
                st.write(message["details"])

# Chat input
if prompt := st.chat_input("Ask something about MetaKGP..."):
    # Add user message to history
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Generate response
    with st.chat_message("assistant"):
        with st.spinner("Thinking through the graph..."):
            try:
                # Perform reasoning
                result = st.session_state.engine.reason(prompt)
                answer = result["answer"]
                path = result["path"]
                knowledge = result["knowledge"]
                verification = result.get("verification", {})

                # Display main answer
                st.markdown(answer)

                # Create a detailed trace for the expander
                details = {
                    "Reasoning Path": path,
                    "MoE Verification": verification,
                    "Knowledge Set": knowledge
                }

                # Visualization of the Thought Graph
                if path:
                    st.markdown("#### Thought Graph")
                    nodes = []
                    edges = []

                    # Build nodes for the path
                    for i, url in enumerate(path):
                        # Try to get a cleaner title from the engine's neo4j utils
                        info = st.session_state.engine.neo4j.get_page_info(url)
                        label = info['title'] if info else url
                        nodes.append(Node(id=url, label=label, size=20, color="#1e88e5"))

                        if i > 0:
                            edges.append(Edge(source=path[i-1], target=url, label="EXPLORED"))

                    # Display the graph
                    agraph(
                        nodes=nodes,
                        edges=edges,
                        device="mobile",
                        height=300
                    )

                # Store in history
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer,
                    "details": details
                })

            except Exception as e:
                st.error(f"An error occurred during reasoning: {e}")
                st.session_state.messages.append({"role": "assistant", "content": f"Error: {e}"})

# Custom function to handle citations in text (simple replacement)
# In a real scenario, we would parse the answer and insert links.
