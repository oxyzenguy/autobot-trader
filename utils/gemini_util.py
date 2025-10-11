import os
import google.generativeai as genai

# API 키를 환경 변수에서 불러옵니다.
API_KEY = os.getenv("GOOGLE_API_KEY")

if not API_KEY:
    print("오류: GOOGLE_API_KEY 환경 변수가 설정되지 않았습니다.")
    print("API 키를 설정해주세요 (예: $env:GOOGLE_API_KEY='YOUR_API_KEY')")
else:
    genai.configure(api_key=API_KEY)
    model = genai.GenerativeModel('gemini-pro')

    # --- 텍스트 생성 ---
    def generate_text(prompt):
        try:
            response = model.generate_content(prompt)
            print("--- 텍스트 생성 결과 ---")
            print(response.text)
        except Exception as e:
            print(f"텍스트 생성 중 오류 발생: {e}")

    # --- 멀티턴 대화 ---
    def start_chat():
        print("--- Gemini 채팅 모드 (종료: 'quit' 또는 Ctrl+C) ---")
        chat = model.start_chat(history=[])
        while True:
            user_input = input("You: ")
            if user_input.lower() == 'quit':
                break
            try:
                response = chat.send_message(user_input)
                print(f"Gemini: {response.text}")
            except Exception as e:
                print(f"채팅 중 오류 발생: {e}")

    # 예시: 터미널 인수에 따라 기능 실행
    import sys
    if __name__ == "__main__":
        if len(sys.argv) > 1:
            command = sys.argv[1]
            if command == "generate" and len(sys.argv) > 2:
                generate_text(" ".join(sys.argv[2:]))
            elif command == "chat":
                start_chat()
            else:
                print("사용법: python gemini_util.py generate \"프롬프트\"")
                print("사용법: python gemini_util.py chat")
        else:
            print("명령어를 입력하세요. (예: generate, chat)")