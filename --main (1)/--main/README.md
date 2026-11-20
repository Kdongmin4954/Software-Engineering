# 당근마켓
소프트웨어 공학 프로젝트

## 채팅 설정

- 백엔드 재시작 후 기존 로그인 세션은 한 번 다시 로그인하면 실시간 채팅과 채팅방 삭제를 사용할 수 있습니다. `backend/.env`에 `CHAT_SESSION_SECRET`을 설정하면 백엔드 재시작 후에도 발급된 채팅 세션을 유지할 수 있습니다.
- 채팅방 삭제에는 Supabase의 `chat_message`와 `chat_participant`에서 `chatroom`을 참조하는 외래 키에 `ON DELETE CASCADE`가 필요합니다. 기존 DB에 이 설정이 없다면 Supabase SQL Editor에서 `backend/chatroom_delete_cascade.sql`을 실행합니다.
