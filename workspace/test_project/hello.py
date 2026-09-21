def binary_search(arr, val, start, end):
    """在已排序区间 [start, end] 中二分查找 val 应插入的位置"""
    while start < end:
        mid = (start + end) // 2
        if arr[mid] < val:
            start = mid + 1
        else:
            end = mid
    return start


def binary_insertion_sort(arr):
    """二分插入排序"""
    for i in range(1, len(arr)):
        key = arr[i]
        # 在已排序的前 i 个元素中二分查找插入位置
        pos = binary_search(arr, key, 0, i)
        # 将 pos 到 i-1 的元素后移一位，腾出位置
        arr[pos+1:i+1] = arr[pos:i]
        arr[pos] = key
    return arr


if __name__ == "__main__":
    data = [37, 23, 0, 17, 12, 72, 31, 46, 100, 88, 54]
    print("原始数据:", data)
    sorted_data = binary_insertion_sort(data)
    print("排序结果:", sorted_data)
