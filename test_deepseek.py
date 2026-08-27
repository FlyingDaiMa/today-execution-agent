def main():
    import os

    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()

    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=os.getenv("DEEPSEEK_BASE_URL"),
    )

    response = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL"),
        messages=[
            {
                "role": "user",
                "content": "只回复：DeepSeek连接成功",
            }
        ],
    )

    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
