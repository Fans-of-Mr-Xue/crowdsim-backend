"""Grounded borrowing suggestions from stored case facts, before user adoption."""

# Each suggestion is conditional on a fact appearing in this case record.
BORROWING_ASPECTS = (
    (('台阶', '瓶颈', '狭窄', '通道'), '检查本场景瓶颈路段的有效宽度、通行能力和排队位置'),
    (('对冲', '双向', '汇流', '交汇'), '将双向对冲或汇流位置纳入流动冲突分析，并对比单向组织、分流情景'),
    (('跨年', '散场', '客流', '人流', '到达'), '梳理活动前后分时到达、驻留与离场变化，识别峰值时段'),
    (('限流', '分流', '疏散', '管控'), '将案例提及的措施作为待比较的管控情景，明确实施时刻和适用通道'),
    (('暴雨', '洪水', '积水', '内涝'), '在涉及降雨或积水的情景中补充通道受阻、避险转移和预警响应条件'),
)


def recommendation_content(doc, *, pre_event=False):
    details = [(str(row[0]), str(row[1])) for row in doc.get('details', [])
               if isinstance(row, list) and len(row) == 2 and row[1]]
    relevant = [f'{label}：{value}' for label, value in details
                if any(word in label for word in ('诱因', '风险', '处置', '措施', '经验', '机制', '数据'))]
    summary = str(doc.get('summary') or doc.get('eventRecord', {}).get('description') or '').strip()
    evidence = '；'.join(relevant[:3]) or summary
    if not evidence:
        return None  # A title alone cannot support a useful recommendation.
    facts = summary + ' ' + ' '.join(value for _, value in details)
    aspects = BORROWING_ASPECTS
    if pre_event:
        aspects = (
            (('台阶', '瓶颈', '狭窄', '通道'), '核对已有路网中瓶颈路段的有效宽度、通行能力和排队位置'),
            (('对冲', '双向', '汇流', '交汇'), '关注已有路网中的汇流位置及拥挤减速，不推定管理措施效果'),
            (('跨年', '散场', '客流', '人流', '到达'), '梳理分时到达、驻留与离场变化，比较峰值时段'),
        )
    suggestions = [suggestion for words, suggestion in aspects if any(word in facts for word in words)]
    if not suggestions:
        suggestions = ['对照案例记录梳理本任务的触发条件、关键过程和需要补充的数据']
    note = '案例依据：' + evidence[:460] + '\n本次可借鉴：\n' + '\n'.join(
        f'{index + 1}. {value}。' for index, value in enumerate(suggestions[:3]))
    note += '\n适用边界：上述内容是建模建议；历史人数、阈值和措施效果不能直接作为本场景参数。'
    return {'adoption_note': note[:1200], 'borrowing_evidence': evidence[:600]}
