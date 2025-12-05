import re
from pypinyin import lazy_pinyin, Style
import struct
import time
from io import BytesIO


def gen_msudp_from_file(input_file):
    """
    从指定格式文件生成 mschxudp 格式的二进制数据
    :param input_file: 输入文件路径，格式为 "编码 排序值 词条"
    :return: bytes 二进制数据
    """
    # 读取并解析输入文件
    table = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # 解析格式: "rlzdzy 1 C)劳动"
            parts = line.split(" ", 2)  # 只分割前两个空格
            if len(parts) < 3:
                continue

            code = parts[0]
            try:
                order = int(parts[1])
            except ValueError:
                order = 1  # 默认值
            word = parts[2]

            table.append({"word": word, "code": code, "order": order})

    # 生成二进制数据
    return gen_msudp(table)


def gen_msudp(table):
    """
    生成 mschxudp 格式的二进制数据
    :param table: 词条列表，每个元素为 dict {'word': str, 'code': str, 'order': int}
    :return: bytes 二进制数据
    """
    buf = BytesIO()
    stamp = struct.pack("<I", int(time.time()))  # 小端序4字节时间戳

    # 1. 写入固定头部 (16字节)
    buf.write(
        bytes(
            [
                0x6D,
                0x73,
                0x63,
                0x68,
                0x78,
                0x75,
                0x64,
                0x70,
                0x02,
                0x00,
                0x60,
                0x00,
                0x01,
                0x00,
                0x00,
                0x00,
            ]
        )
    )

    # 2. 写入元数据
    buf.write(struct.pack("<I", 0x40))  # 固定值 0x40
    buf.write(struct.pack("<I", 0x40 + 4 * len(table)))  # 索引区结束位置
    buf.write(b"\x00" * 4)  # 预留文件总长度(稍后填充)
    buf.write(struct.pack("<I", len(table)))  # 词条数量
    buf.write(stamp)  # 时间戳
    buf.write(b"\x00" * 28)  # 28字节预留空位
    buf.write(b"\x00" * 4)  # 4字节预留空位

    # 3. 准备词条数据并计算偏移量
    encoded_data = []
    for entry in table:
        # UTF-16LE 编码 (小端序UTF-16，无BOM)
        word_enc = entry["word"].encode("utf-16le")
        code_enc = entry["code"].encode("utf-16le")
        encoded_data.append(
            {"word": word_enc, "code": code_enc, "order": entry["order"]}
        )

    # 写入词条偏移量 (最后一个词条不写偏移量)
    offset_sum = 0
    for i in range(len(encoded_data) - 1):
        # 当前词条长度 = 编码长 + 词长 + 20字节固定头
        entry_len = len(encoded_data[i]["code"]) + len(encoded_data[i]["word"]) + 20
        offset_sum += entry_len
        buf.write(struct.pack("<I", offset_sum))

    # 4. 写入词条主体数据
    for data in encoded_data:
        buf.write(bytes([0x10, 0x00, 0x10, 0x00]))  # 固定头

        # 编码长度 + 18 (2字节小端序)
        code_len_plus_18 = len(data["code"]) + 18
        buf.write(struct.pack("<H", code_len_plus_18))

        buf.write(bytes([data["order"], 0x06]))  # 排序值和固定标记
        buf.write(b"\x00" * 4)  # 4字节空位
        buf.write(stamp)  # 时间戳
        buf.write(data["code"])  # 编码字节
        buf.write(b"\x00\x00")  # 结束符
        buf.write(data["word"])  # 词文本字节
        buf.write(b"\x00\x00")  # 结束符

    # 5. 更新文件总长度 (位置0x18)
    result = buf.getvalue()
    total_len = struct.pack("<I", len(result))
    result = result[:0x18] + total_len + result[0x18 + 4 :]

    return result


def get_chinese_chars(text):
    """提取字符串中的所有中文字符列表"""
    return re.findall(r"[\u4e00-\u9fa5]", text)


def get_pinyin_initials(char_list):
    """将汉字列表转换为拼音首字母字符串"""
    result = ""
    for char in char_list:
        py = lazy_pinyin(char, style=Style.FIRST_LETTER)
        if py:
            result += py[0]
        else:
            result += char
    return result


def generate_id(question_text):
    """
    生成ID规则：前三个中文字符拼音首字母 + 最后三个中文字符拼音首字母
    """
    # 1. 截取题干部分，防止把选项读进去
    split_pattern = re.compile(r"(A\)|A\.|正确答案|你的答案|本题得分)")
    split_match = split_pattern.search(question_text)
    if split_match:
        stem_text = question_text[: split_match.start()]
    else:
        stem_text = question_text

    # 2. 提取中文
    cn_chars = get_chinese_chars(stem_text)

    if not cn_chars:
        return "NULL"

    # 3. 前三字 + 后三字
    head_chars = cn_chars[:3]
    tail_chars = cn_chars[-3:]

    # 4. 拼音首字母拼接
    return f"{get_pinyin_initials(head_chars)}{get_pinyin_initials(tail_chars)}"


def clean_text(text):
    """去除换行和多余空格"""
    if not text:
        return ""
    # 替换换行符为空格，去除首尾空白
    return text.replace("\n", " ").replace("\r", "").strip()


def parse_options(block_text):
    """
    解析题目块中的选项，返回字典 { 'A': '选项内容', 'B': '选项内容' ... }
    """
    options = {}
    # 正则匹配行首的选项，如 "A) xxx" 或 "A. xxx"
    # (?:^|\n) 确保匹配行首
    # \s* 允许前面有空格
    # ([A-Z]) 捕获字母
    # [\.\)] 匹配点或括号
    # \s* (.*?) 捕获内容
    pattern = re.compile(
        r"(?:^|\n)\s*([A-Z])[\.\)]\s*(.*?)(?=(?:^|\n)\s*[A-Z][\.\)]|(?:^|\n)\s*正确答案|$)"
    )

    # 由于正则可能跨行匹配困难，这里采用逐行扫描的方式辅助
    lines = block_text.split("\n")
    current_opt = None

    # 简单的正则用于提取单行选项
    line_pattern = re.compile(r"^\s*([A-Z])[\.\)]\s*(.*)")

    for line in lines:
        match = line_pattern.match(line)
        if match:
            current_opt = match.group(1)
            content = match.group(2).strip()
            options[current_opt] = content
        elif current_opt and "正确答案" not in line and "本小题得分" not in line:
            # 如果是选项的续行（选项内容很长换行了），拼接到上一个选项
            # 这里简单处理，假设大部分选项是一行的
            pass

    return options


def process_exam_data(input_file, output_file):
    with open(input_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 切割题目块
    pattern = re.compile(r"(?:^|\n)\s*(\d+)\.(.*?)(?=(?:^|\n)\s*\d+\.|$)", re.DOTALL)
    matches = pattern.findall(content)

    results = []

    for match in matches:
        full_block = match[1]

        # --- 1. 生成 ID ---
        q_id = generate_id(full_block)

        # --- 2. 提取答案和选项 ---
        final_answer_str = "未找到"

        # 尝试提取 "正确答案" (通常是 A, B, C...)
        obj_match = re.search(
            r"正确答案为[：:]\s*([A-Z\s]+?)(?=\s+你|[\n\r]|$)", full_block
        )

        # 尝试提取 "参考答案" (主观题/填空题)
        subj_match = re.search(
            r"参考答案[：:]\s*([\s\S]*?)\s*(?:范文点评|本小题得分|$)", full_block
        )

        # 尝试解析选项内容
        options_map = parse_options(full_block)

        if obj_match and options_map:
            # === 情况A：有选项的选择题/判断题 ===
            raw_ans_letters = obj_match.group(1).strip()
            # 分割字母，比如 "A B" -> ['A', 'B']
            # 使用正则分割，防止中间有奇怪的符号
            ans_list = re.findall(r"[A-Z]", raw_ans_letters)

            combined_ans = []
            for letter in ans_list:
                # 获取该字母对应的选项内容，如果没有找到内容则只保留字母
                opt_content = options_map.get(letter, "")
                # 拼接格式：A选项内容
                combined_ans.append(f"{letter}{opt_content}")

            # 将多个答案拼接 (如 A内容A B内容B)
            # 为了保证作为一列输出，中间不建议加空格，或者加紧凑的符号
            final_answer_str = "".join(combined_ans)

        elif obj_match:
            # === 情况B：有答案字母但没提取到选项文本（可能是填空题误判，或者格式特殊）===
            final_answer_str = obj_match.group(1).strip().replace(" ", "")

        elif subj_match:
            # === 情况C：主观题/填空题（读取参考答案）===
            final_answer_str = clean_text(subj_match.group(1))

        else:
            # === 情况D：填空题（有的填空题格式是“正确答案为：xxx”而不是A/B）===
            # 这种情况下上面的 obj_match 可能会匹配到中文
            # 重新尝试匹配任意字符的正确答案
            text_ans_match = re.search(
                r"正确答案为[：:]\s*(.*?)(?=\s+你|[\n\r]|$)", full_block
            )
            if text_ans_match:
                final_answer_str = clean_text(text_ans_match.group(1))

        # 格式化输出：ID 1 答案
        results.append(f"{q_id} 1 {final_answer_str}")

    # 写入文件
    with open(output_file, "w", encoding="utf-8") as f:
        for line in results:
            f.write(line + "\n")

    print(f"处理完成！结果已保存至 {output_file}")


if __name__ == "__main__":
    # input_file = "马克思基本原理题库2.txt"
    input_file = input("请输入文件名：")
    output_file_txt = "output_" + input_file + ".txt"
    output_file_dat = "output_" + input_file + ".dat"
    try:
        process_exam_data(input_file, output_file_txt)
        binary_data = gen_msudp_from_file(output_file_txt)
        with open(output_file_dat, "wb") as f:
            f.write(binary_data)
        print(f"成功生成词库文件: {output_file_dat}")
    except FileNotFoundError:
        print("未找到 input.txt，请创建文件。")
