为角色 {{ subject.name }} 生成 {{ vars.today.date }} 的日程。

角色设定：{{ vars.role.persona }}
当前情绪：{{ vars.mood.mood }}（精力 {{ vars.mood.energy }}）

要求：
1. 每段日程写清时间、时长、地点、一句话理由；
2. 精力低于 50 时，把高强度活动往后放；
3. 只输出一个 ```result JSON 块，字段 plan_path（产物路径，字符串）与 conflict_count（冲突条数，整数）。
