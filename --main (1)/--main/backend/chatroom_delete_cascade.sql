-- Run against databases where the chatroom foreign keys do not cascade.
begin;

do $$
declare
    child_table regclass;
    chatroom_fk record;
    has_cascade boolean;
begin
    foreach child_table in array array['public.chat_message'::regclass, 'public.chat_participant'::regclass]
    loop
        has_cascade := false;
        for chatroom_fk in
            select conname, confdeltype
            from pg_constraint
            where contype = 'f'
              and conrelid = child_table
              and confrelid = 'public.chatroom'::regclass
              and conkey = array[(select attnum from pg_attribute
                                  where attrelid = child_table and attname = 'chatroom_id')]
        loop
            if chatroom_fk.confdeltype = 'c' then
                has_cascade := true;
            else
                execute format('alter table %s drop constraint %I', child_table, chatroom_fk.conname);
            end if;
        end loop;

        if not has_cascade then
            execute format(
                'alter table %s add foreign key (chatroom_id) references public.chatroom (chatroom_id) on delete cascade',
                child_table
            );
        end if;
    end loop;
end;
$$;

commit;
