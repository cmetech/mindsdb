import os

import ollama
import pytest

from ..executor_test_base import BaseExecutorTest

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANYSCALE_API_KEY = os.environ.get("ANYSCALE_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")


def ollama_model_exists(model_name: str) -> bool:
    try:
        ollama.show(model_name)
        return True
    except Exception:
        return False


class TestLangchain(BaseExecutorTest):
    """Test Class for Langchain Integration Testing"""
    @pytest.fixture(autouse=True, scope="function")
    def setup_method(self):
        """Setup test environment, creating a project"""
        super().setup_method()
        self.run_sql("create database proj")

    @pytest.mark.skipif(OPENAI_API_KEY is None, reason='Missing OpenAI API key (OPENAI_API_KEY env variable)')
    def test_default_provider(self):
        self.run_sql(
            f"""
           create model proj.test_conversational_model
           predict answer
           using
             engine='langchain',
             prompt_template='Answer the user in a useful way: {{{{question}}}}',
             openai_api_key='{OPENAI_API_KEY}';
        """
        )
        self.wait_predictor("proj", "test_conversational_model")

        result_df = self.run_sql(
            """
            SELECT answer
            FROM proj.test_conversational_model
            WHERE question='What is the capital of Sweden?'
        """
        )
        assert "stockholm" in result_df['answer'].iloc[0].lower()

    @pytest.mark.skipif(ANTHROPIC_API_KEY is None, reason='Missing Anthropic API key (ANTHROPIC_API_KEY env variable)')
    def test_anthropic_provider(self):
        self.run_sql(
            f"""
           create model proj.test_anthropic_langchain_model
           predict answer
           using
             engine='langchain',
             model_name='claude-2.1',
             prompt_template='Answer the user in a useful way: {{{{question}}}}',
             anthropic_api_key='{ANTHROPIC_API_KEY}';
        """
        )
        self.wait_predictor("proj", "test_anthropic_langchain_model")

        result_df = self.run_sql(
            """
            SELECT answer
            FROM proj.test_anthropic_langchain_model
            WHERE question='What is the capital of Sweden?'
        """
        )
        assert "stockholm" in result_df['answer'].iloc[0].lower()

    @pytest.mark.skipif(not ollama_model_exists('mistral'), reason='Make sure the mistral model is available locally by running `ollama pull mistral`')
    def test_ollama_provider(self):
        self.run_sql(
            """
           create model proj.test_ollama_model
           predict answer
           using
             engine='langchain',
             model_name='mistral',
             prompt_template='Answer the user in a useful way: {{question}}'
            """
        )
        self.wait_predictor("proj", "test_ollama_model")

        result_df = self.run_sql(
            """
            SELECT answer
            FROM proj.test_ollama_model
            WHERE question='What is the capital of British Columbia, Canada?'
        """
        )
        assert "victoria" in result_df['answer'].iloc[0].lower()


    @pytest.mark.skipif(GOOGLE_API_KEY is None, reason='Missing Google API key (GOOGLE_API_KEY env variable)')
    def test_google_provider(self):
        self.run_sql(
            f"""
           create model proj.test_google_langchain_model
           predict answer
           using
             engine='langchain',
             provider='google',
             model_name='gemini-1.5-pro',
             prompt_template='Answer the user in a useful way: {{{{question}}}}',
             google_api_key='{GOOGLE_API_KEY}';
        """
        )
        self.wait_predictor("proj", "test_google_langchain_model")

        result_df = self.run_sql(
            """
            SELECT answer
            FROM proj.test_google_langchain_model
            WHERE question='What is the capital of Sweden?'
        """
        )
        assert "stockholm" in result_df['answer'].iloc[0].lower()

    def test_describe(self):
        self.run_sql(
            """
           create model proj.test_describe_model
           predict answer
           using
             engine='langchain',
             prompt_template='Answer the user in a useful way: {{question}}';
        """
        )
        self.wait_predictor("proj", "test_describe_model")
        result_df = self.run_sql('DESCRIBE proj.test_describe_model')
        assert not result_df.empty

    @pytest.mark.skipif(OPENAI_API_KEY is None, reason='Missing OpenAI API key (OPENAI_API_KEY env variable)')
    def test_prompt_template_args(self):
        self.run_sql(
            f"""
           create model proj.test_prompt_template_model
           predict answer
           using
             engine='langchain',
             prompt_template='Your name is {{{{name}}}}. Answer the user in a useful way: {{{{question}}}}',
             openai_api_key='{OPENAI_API_KEY}';
        """
        )
        self.wait_predictor("proj", "test_prompt_template_model")

        agent_name = 'professor farnsworth'
        result_df = self.run_sql(
            f"""
            SELECT answer
            FROM proj.test_prompt_template_model
            WHERE question='What is your name?' AND name='{agent_name}'
        """
        )
        assert agent_name in result_df['answer'].iloc[0].lower()
